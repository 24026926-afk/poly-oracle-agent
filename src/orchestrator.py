#!/usr/bin/env python3
"""
src/orchestrator.py

Main entry point and orchestrator for poly-oracle-agent.
Wires together Ingestion, Context, Evaluation, and Execution nodes
using asyncio queues.
"""

import asyncio
import sys
from typing import Any

import aiohttp
import httpx
import structlog
from dotenv import load_dotenv
from web3 import AsyncHTTPProvider, AsyncWeb3

from src.agents.context.aggregator import DataAggregator
from src.agents.context.prompt_factory import PromptFactory
from src.agents.evaluation.claude_client import ClaudeClient
from src.agents.execution.bankroll_tracker import BankrollPortfolioTracker
from src.agents.execution.broadcaster import OrderBroadcaster
from src.agents.execution.gas_estimator import GasEstimator
from src.agents.execution.nonce_manager import NonceManager
from src.agents.execution.signer import TransactionSigner
from src.agents.ingestion.market_discovery import MarketDiscoveryEngine
from src.agents.ingestion.rest_client import GammaRESTClient
from src.agents.ingestion.ws_client import CLOBWebSocketClient
from src.core.config import AppConfig, get_config
from src.db.engine import AsyncSessionLocal, engine

# Ensure .env is explicitly loaded if running from root
load_dotenv()

# Configure structlog for the root execution
structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer(),
    ]
)

logger = structlog.get_logger(__name__)


class Orchestrator:
    """Top-level coordinator for the 4-layer async pipeline."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.active_condition_id: str | None = None

        # Queue wiring per architecture sequence:
        # ingestion -> context -> evaluation -> execution
        self.market_queue: asyncio.Queue[Any] = asyncio.Queue()
        self.prompt_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.execution_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        self._http_session: aiohttp.ClientSession | None = None
        self._httpx_client: httpx.AsyncClient | None = None
        self._tasks: list[asyncio.Task[Any]] = []

        self.w3 = AsyncWeb3(AsyncHTTPProvider(self.config.polygon_rpc_url))
        self.signer = TransactionSigner(config=self.config)
        self.nonce_manager = NonceManager(
            self.w3, self.config.wallet_address, dry_run=self.config.dry_run,
        )
        self.gas_estimator = GasEstimator(self.w3)
        self.bankroll_tracker = BankrollPortfolioTracker(
            config=self.config,
            db_session_factory=AsyncSessionLocal,
        )

        self.ws_client = CLOBWebSocketClient(
            config=self.config,
            queue=self.market_queue,
            db_session_factory=AsyncSessionLocal,
        )
        self.prompt_factory = PromptFactory()
        self.claude_client = ClaudeClient(
            in_queue=self.prompt_queue,
            out_queue=self.execution_queue,
            config=self.config,
        )

        # Initialized in start() after discovery
        self.aggregator: DataAggregator | None = None
        self.gamma_client: GammaRESTClient | None = None
        self.discovery_engine: MarketDiscoveryEngine | None = None
        self.broadcaster: OrderBroadcaster | None = None

    async def start(self) -> None:
        """Start all layers and run until cancelled."""
        logger.info("orchestrator.starting")

        self._http_session = aiohttp.ClientSession()
        self._httpx_client = httpx.AsyncClient()
        self.gamma_client = GammaRESTClient(
            config=self.config,
            http_session=self._httpx_client,
        )
        self.discovery_engine = MarketDiscoveryEngine(
            gamma_client=self.gamma_client,
            bankroll_tracker=self.bankroll_tracker,
            config=self.config,
        )
        self.broadcaster = OrderBroadcaster(
            w3=self.w3,
            nonce_manager=self.nonce_manager,
            gas_estimator=self.gas_estimator,
            http_session=self._http_session,
            db_session_factory=AsyncSessionLocal,
            clob_rest_url=self.config.clob_rest_url,
            config=self.config,
            bankroll_tracker=self.bankroll_tracker,
        )
        await self.nonce_manager.initialize()

        # Discover eligible markets before wiring the pipeline
        eligible = await self.discovery_engine.discover()
        if not eligible:
            logger.warning(
                "orchestrator.no_eligible_markets_at_startup",
            )
            return
        self.active_condition_id = eligible[0]
        logger.info(
            "orchestrator.market_selected",
            condition_id=self.active_condition_id,
        )

        self.aggregator = DataAggregator(
            input_queue=self.market_queue,
            output_queue=self.prompt_queue,
            condition_id=self.active_condition_id,
        )

        self._tasks = [
            asyncio.create_task(self.ws_client.run(), name="IngestionTask"),
            asyncio.create_task(self.aggregator.start(), name="ContextTask"),
            asyncio.create_task(self.claude_client.start(), name="EvaluationTask"),
            asyncio.create_task(
                self._execution_consumer_loop(),
                name="ExecutionTask",
            ),
            asyncio.create_task(
                self._discovery_loop(),
                name="DiscoveryTask",
            ),
        ]

        try:
            await asyncio.gather(*self._tasks)
        except asyncio.CancelledError:
            logger.info("orchestrator.cancelled")
            raise
        finally:
            await self.shutdown()

    async def _execution_consumer_loop(self) -> None:
        """Consume approved decisions and broadcast signed orders."""
        while True:
            item = await self.execution_queue.get()
            try:
                if self.broadcaster is None:
                    logger.error("execution.broadcaster_not_initialized")
                    continue

                if self.config.dry_run:
                    eval_resp = item.get("evaluation")
                    logger.info(
                        "execution.dry_run_skip",
                        dry_run=True,
                        condition_id=str(
                            eval_resp.market_context.condition_id
                            if eval_resp else "unknown"
                        ),
                        proposed_action=(
                            eval_resp.recommended_action.value
                            if eval_resp else "unknown"
                        ),
                        would_be_size_usdc=(
                            str(eval_resp.position_size_pct)
                            if eval_resp else "unknown"
                        ),
                    )
                    continue

                signed_order = await self.signer.build_order_from_decision(
                    item,
                    bankroll_tracker=self.bankroll_tracker,
                )
                decision_id = str(item.get("snapshot_id", "unknown"))
                await self.broadcaster.broadcast(
                    signed_order=signed_order,
                    decision_id=decision_id,
                )
            except Exception as exc:
                logger.error("execution.consumer_error", error=str(exc))
            finally:
                self.execution_queue.task_done()

    async def _discovery_loop(self) -> None:
        """Re-run market discovery every 5 minutes to rotate if needed."""
        while True:
            await asyncio.sleep(300)
            try:
                if self.discovery_engine is None:
                    continue
                eligible = await self.discovery_engine.discover()
                if eligible and eligible[0] != self.active_condition_id:
                    logger.info(
                        "orchestrator.market_rotation",
                        old_condition_id=self.active_condition_id,
                        new_condition_id=eligible[0],
                    )
                    self.active_condition_id = eligible[0]
                    if self.aggregator is not None:
                        self.aggregator.condition_id = eligible[0]
                        self.aggregator.best_bid = 0.0
                        self.aggregator.best_ask = 0.0
                        self.aggregator._last_emitted_midpoint = None
                elif not eligible:
                    logger.warning(
                        "orchestrator.no_eligible_markets_on_refresh",
                        keeping=self.active_condition_id,
                    )
            except Exception as exc:
                logger.error(
                    "orchestrator.discovery_loop_error",
                    error=str(exc),
                )

    async def shutdown(self) -> None:
        """Stop running components, cancel tasks, and dispose shared resources."""
        logger.info("orchestrator.shutdown_start")

        for stoppable in (self.aggregator, self.claude_client):
            if stoppable is None:
                continue
            try:
                await stoppable.stop()
            except Exception as exc:
                logger.warning("orchestrator.stop_failed", component=type(stoppable).__name__, error=str(exc))

        for task in self._tasks:
            if not task.done():
                task.cancel()

        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks = []

        if self._httpx_client is not None:
            await self._httpx_client.aclose()
            self._httpx_client = None

        if self._http_session is not None:
            await self._http_session.close()
            self._http_session = None

        await engine.dispose()
        logger.info("orchestrator.shutdown_complete")


async def main() -> None:
    """Application entrypoint."""
    logger.info("Initializing Poly-Oracle-Agent Orchestrator...")
    try:
        config = get_config()
    except Exception as exc:
        logger.error("Configuration validation failed.", error=str(exc))
        sys.exit(1)

    orchestrator = Orchestrator(config)
    await orchestrator.start()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
