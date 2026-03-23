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
from src.agents.execution.broadcaster import OrderBroadcaster
from src.agents.execution.gas_estimator import GasEstimator
from src.agents.execution.nonce_manager import NonceManager
from src.agents.execution.signer import TransactionSigner
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

        # TODO: replace with dynamic market selection from WI-03
        self.asset_id = (
            "0x2173a110cb1ba3c7b39912066c07dd82a4664b5953dd4305bc8c3e03cd530e8c"
        )

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

        self.ws_client = CLOBWebSocketClient(
            config=self.config,
            queue=self.market_queue,
            db_session_factory=AsyncSessionLocal,
        )
        self.aggregator = DataAggregator(
            input_queue=self.market_queue,
            output_queue=self.prompt_queue,
            condition_id=self.asset_id,
        )
        self.prompt_factory = PromptFactory()
        self.claude_client = ClaudeClient(
            in_queue=self.prompt_queue,
            out_queue=self.execution_queue,
            config=self.config,
        )

        self.gamma_client: GammaRESTClient | None = None
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
        self.broadcaster = OrderBroadcaster(
            w3=self.w3,
            nonce_manager=self.nonce_manager,
            gas_estimator=self.gas_estimator,
            http_session=self._http_session,
            db_session_factory=AsyncSessionLocal,
            clob_rest_url=self.config.clob_rest_url,
            config=self.config,
        )
        await self.nonce_manager.initialize()

        self._tasks = [
            asyncio.create_task(self.ws_client.run(), name="IngestionTask"),
            asyncio.create_task(self.aggregator.start(), name="ContextTask"),
            asyncio.create_task(self.claude_client.start(), name="EvaluationTask"),
            asyncio.create_task(
                self._execution_consumer_loop(),
                name="ExecutionTask",
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

                signed_order = self.signer.build_order_from_decision(item)
                decision_id = str(item.get("snapshot_id", "unknown"))
                await self.broadcaster.broadcast(
                    signed_order=signed_order,
                    decision_id=decision_id,
                )
            except Exception as exc:
                logger.error("execution.consumer_error", error=str(exc))
            finally:
                self.execution_queue.task_done()

    async def shutdown(self) -> None:
        """Stop running components, cancel tasks, and dispose shared resources."""
        logger.info("orchestrator.shutdown_start")

        for stoppable in (self.aggregator, self.claude_client):
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
