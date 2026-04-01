#!/usr/bin/env python3
"""
src/orchestrator.py

Main entry point and orchestrator for poly-oracle-agent.
Wires together Ingestion, Context, Evaluation, and Execution nodes
using asyncio queues.
"""

import asyncio
from decimal import Decimal
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
from src.agents.execution.bankroll_sync import BankrollSyncProvider
from src.agents.execution.bankroll_tracker import BankrollPortfolioTracker
from src.agents.execution.broadcaster import OrderBroadcaster
from src.agents.execution.alert_engine import AlertEngine
from src.agents.execution.circuit_breaker import CircuitBreaker, CircuitBreakerState
from src.agents.execution.execution_router import ExecutionRouter
from src.agents.execution.exit_order_router import ExitOrderRouter
from src.agents.execution.exit_strategy_engine import ExitStrategyEngine
from src.agents.execution.gas_estimator import GasEstimator
from src.agents.execution.lifecycle_reporter import PositionLifecycleReporter
from src.agents.execution.nonce_manager import NonceManager
from src.agents.execution.pnl_calculator import PnLCalculator
from src.agents.execution.portfolio_aggregator import PortfolioAggregator
from src.agents.execution.polymarket_client import PolymarketClient
from src.agents.execution.position_tracker import PositionTracker
from src.agents.execution.signer import TransactionSigner
from src.agents.execution.telegram_notifier import TelegramNotifier
from src.agents.ingestion.market_discovery import MarketDiscoveryEngine
from src.agents.ingestion.rest_client import GammaRESTClient
from src.agents.ingestion.ws_client import CLOBWebSocketClient
from src.core.config import AppConfig, get_config
from src.db.models import Position
from src.db.engine import AsyncSessionLocal, engine
from src.db.repositories.position_repository import PositionRepository
from src.schemas.execution import (
    ExecutionAction,
    ExecutionResult,
    ExitOrderAction,
    PositionRecord,
    PositionStatus,
)
from src.schemas.risk import LifecycleReport, PortfolioSnapshot

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
        self.nonce_manager = NonceManager(
            self.w3, self.config.wallet_address, dry_run=self.config.dry_run,
        )
        self.gas_estimator = GasEstimator(self.w3)
        self.bankroll_sync = BankrollSyncProvider(config=self.config)
        self.bankroll_tracker = BankrollPortfolioTracker(
            config=self.config,
            db_session_factory=AsyncSessionLocal,
            bankroll_sync=self.bankroll_sync,
        )
        self.polymarket_client = PolymarketClient(host=self.config.clob_rest_url)

        # WI-15: signer constructed only when not in dry_run mode.
        # dry_run=True → no key material loaded, no signer instantiated.
        self.signer: TransactionSigner | None = None
        if not self.config.dry_run:
            self.signer = TransactionSigner(config=self.config)
        self.execution_router = ExecutionRouter(
            config=self.config,
            polymarket_client=self.polymarket_client,
            bankroll_provider=self.bankroll_sync,
            transaction_signer=self.signer,
        )
        self.position_tracker = PositionTracker(
            config=self.config,
            db_session_factory=AsyncSessionLocal,
        )
        self.exit_strategy_engine = ExitStrategyEngine(
            config=self.config,
            polymarket_client=self.polymarket_client,
            db_session_factory=AsyncSessionLocal,
        )
        self.exit_order_router = ExitOrderRouter(
            config=self.config,
            polymarket_client=self.polymarket_client,
            transaction_signer=self.signer,
        )
        self.pnl_calculator = PnLCalculator(
            config=self.config,
            db_session_factory=AsyncSessionLocal,
        )
        self.portfolio_aggregator = PortfolioAggregator(
            config=self.config,
            polymarket_client=self.polymarket_client,
            db_session_factory=AsyncSessionLocal,
        )
        self.lifecycle_reporter = PositionLifecycleReporter(
            config=self.config,
            db_session_factory=AsyncSessionLocal,
        )
        self.alert_engine = AlertEngine(config=self.config)
        self._telegram_client: httpx.AsyncClient | None = None
        self.telegram_notifier: TelegramNotifier | None = None
        if (
            self.config.enable_telegram_notifier
            and self.config.telegram_bot_token.get_secret_value()
            and self.config.telegram_chat_id
        ):
            self._telegram_client = httpx.AsyncClient()
            self.telegram_notifier = TelegramNotifier(
                config=self.config,
                http_client=self._telegram_client,
            )
        else:
            logger.info("telegram.disabled")

        self.circuit_breaker: CircuitBreaker | None = None
        if self.config.enable_circuit_breaker:
            self.circuit_breaker = CircuitBreaker(config=self.config)
        else:
            logger.info("circuit_breaker.disabled")

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
            db_session_factory=AsyncSessionLocal,
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
            asyncio.create_task(
                self._exit_scan_loop(),
                name="ExitScanTask",
            ),
        ]
        if self.config.enable_portfolio_aggregator:
            self._tasks.append(
                asyncio.create_task(
                    self._portfolio_aggregation_loop(),
                    name="PortfolioAggregatorTask",
                )
            )

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

                eval_resp = item.get("evaluation")
                if eval_resp is None:
                    logger.error("execution.missing_evaluation")
                    continue

                condition_id = str(eval_resp.market_context.condition_id)
                if (
                    self.circuit_breaker is not None
                    and not self.circuit_breaker.check_entry_allowed()
                ):
                    logger.warning(
                        "circuit_breaker.entry_blocked",
                        condition_id=condition_id,
                    )
                    execution_result = ExecutionResult(
                        action=ExecutionAction.SKIP,
                        reason="circuit_breaker_open",
                    )
                else:
                    execution_result = await self.execution_router.route(
                        response=eval_resp,
                        market_context=eval_resp.market_context,
                    )
                item["execution_result"] = execution_result

                yes_token_id = item.get("yes_token_id")
                if yes_token_id is None:
                    yes_token_id = getattr(eval_resp.market_context, "yes_token_id", None)
                if yes_token_id is None:
                    logger.warning(
                        "execution.position_tracking_skipped_missing_yes_token_id",
                        condition_id=condition_id,
                    )
                else:
                    try:
                        await self.position_tracker.record_execution(
                            result=execution_result,
                            condition_id=condition_id,
                            token_id=str(yes_token_id),
                        )
                    except Exception as exc:
                        logger.error(
                            "execution.position_tracking_error",
                            error=str(exc),
                        )

                if (
                    self.telegram_notifier is not None
                    and execution_result.action
                    in (ExecutionAction.EXECUTED, ExecutionAction.DRY_RUN)
                ):
                    order_size = (
                        str(execution_result.order_size_usdc)
                        if execution_result.order_size_usdc is not None
                        else "unknown"
                    )
                    try:
                        await self.telegram_notifier.send_execution_event(
                            summary=(
                                f"BUY ROUTED: {condition_id} | "
                                f"{order_size} USDC | "
                                f"action={execution_result.action.value}"
                            ),
                            dry_run=self.config.dry_run,
                        )
                    except Exception:
                        pass

                if self.config.dry_run:
                    logger.info(
                        "execution.dry_run_skip",
                        dry_run=True,
                        condition_id=condition_id,
                        proposed_action=eval_resp.recommended_action.value,
                        would_be_size_usdc=(
                            str(execution_result.order_size_usdc)
                            if execution_result.order_size_usdc is not None
                            else "unknown"
                        ),
                    )
                    continue

                if execution_result.signed_order is None:
                    logger.error(
                        "execution.signed_order_missing",
                        condition_id=condition_id,
                        action=execution_result.action.value,
                        reason=execution_result.reason,
                    )
                    continue

                decision_id = str(item.get("snapshot_id", "unknown"))
                await self.broadcaster.broadcast(
                    signed_order=execution_result.signed_order,
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

    async def _exit_scan_loop(self) -> None:
        """Run periodic open-position exit scans independent of execution flow."""
        while True:
            await asyncio.sleep(float(self.config.exit_scan_interval_seconds))
            try:
                results = await self.exit_strategy_engine.scan_open_positions()
                for exit_result in results:
                    if not exit_result.should_exit:
                        continue

                    try:
                        position = await self._fetch_position_record(
                            exit_result.position_id
                        )
                        if position is None:
                            logger.warning(
                                "exit_scan.position_not_found",
                                position_id=exit_result.position_id,
                                condition_id=exit_result.condition_id,
                            )
                            continue

                        exit_order_result = await self.exit_order_router.route_exit(
                            exit_result=exit_result,
                            position=position,
                        )
                    except Exception as exc:
                        logger.error(
                            "exit_scan.routing_error",
                            position_id=exit_result.position_id,
                            error=str(exc),
                        )
                        continue

                    if (
                        exit_order_result.action in (
                            ExitOrderAction.SELL_ROUTED,
                            ExitOrderAction.DRY_RUN,
                        )
                        and exit_order_result.exit_price is not None
                    ):
                        try:
                            await self.pnl_calculator.settle(
                                position=position,
                                exit_price=exit_order_result.exit_price,
                            )
                        except Exception as exc:
                            logger.error(
                                "exit_scan.pnl_settlement_error",
                                position_id=exit_result.position_id,
                                error=str(exc),
                            )

                    if (
                        self.telegram_notifier is not None
                        and exit_order_result.action
                        in (ExitOrderAction.SELL_ROUTED, ExitOrderAction.DRY_RUN)
                    ):
                        exit_price = (
                            str(exit_order_result.exit_price)
                            if exit_order_result.exit_price is not None
                            else "unknown"
                        )
                        try:
                            await self.telegram_notifier.send_execution_event(
                                summary=(
                                    f"SELL ROUTED: {exit_result.position_id} | "
                                    f"exit_price={exit_price} | "
                                    f"action={exit_order_result.action.value}"
                                ),
                                dry_run=self.config.dry_run,
                            )
                        except Exception:
                            pass

                    if (
                        exit_order_result.action == ExitOrderAction.SELL_ROUTED
                        and exit_order_result.signed_order is not None
                        and not self.config.dry_run
                        and self.broadcaster is not None
                    ):
                        try:
                            await self.broadcaster.broadcast(
                                signed_order=exit_order_result.signed_order,
                                decision_id=f"exit_{exit_result.position_id}",
                            )
                        except Exception as exc:
                            logger.error(
                                "exit_scan.broadcast_error",
                                position_id=exit_result.position_id,
                                error=str(exc),
                            )

                exits = sum(1 for result in results if result.should_exit)
                holds = len(results) - exits
                logger.info(
                    "exit_scan_loop.completed",
                    total=len(results),
                    exits=exits,
                    holds=holds,
                    interval_seconds=str(self.config.exit_scan_interval_seconds),
                )
            except Exception as exc:
                logger.error(
                    "exit_scan_loop.error",
                    error=str(exc),
                )

    async def _portfolio_aggregation_loop(self) -> None:
        """Periodic portfolio snapshot, lifecycle report, and alert evaluation (WI-23/24/25)."""
        while True:
            await asyncio.sleep(
                float(self.config.portfolio_aggregation_interval_sec)
            )
            snapshot: PortfolioSnapshot | None = None
            report: LifecycleReport | None = None
            try:
                snapshot = await self.portfolio_aggregator.compute_snapshot()
            except Exception as exc:
                logger.error(
                    "portfolio_aggregation_loop.error",
                    error=str(exc),
                )
            try:
                report = await self.lifecycle_reporter.generate_report()
            except Exception as exc:
                logger.error(
                    "lifecycle_report_loop.error",
                    error=str(exc),
                )
            if snapshot is not None and report is not None:
                try:
                    alerts = self.alert_engine.evaluate(snapshot, report)
                    if alerts:
                        logger.warning(
                            "alert_engine.alerts_fired",
                            alert_count=len(alerts),
                            rules=[alert.rule_name for alert in alerts],
                            severities=[alert.severity.value for alert in alerts],
                            dry_run=snapshot.dry_run,
                        )
                        if self.telegram_notifier is not None:
                            for alert in alerts:
                                try:
                                    await self.telegram_notifier.send_alert(alert)
                                except Exception:
                                    pass
                        if self.circuit_breaker is not None:
                            try:
                                previous_state = self.circuit_breaker.state
                                self.circuit_breaker.evaluate_alerts(alerts)
                                if (
                                    previous_state == CircuitBreakerState.CLOSED
                                    and self.circuit_breaker.state
                                    == CircuitBreakerState.OPEN
                                    and self.telegram_notifier is not None
                                ):
                                    try:
                                        await self.telegram_notifier.send_execution_event(
                                            summary=(
                                                "CIRCUIT BREAKER TRIPPED: "
                                                "BUY routing halted due to CRITICAL "
                                                "drawdown alert. Manual reset required."
                                            ),
                                            dry_run=self.config.dry_run,
                                        )
                                    except Exception:
                                        pass
                            except Exception as exc:
                                logger.error(
                                    "circuit_breaker.evaluate_error",
                                    error=str(exc),
                                )
                    else:
                        logger.info(
                            "alert_engine.all_clear",
                            dry_run=snapshot.dry_run,
                        )
                        if self.circuit_breaker is not None:
                            try:
                                self.circuit_breaker.evaluate_alerts([])
                            except Exception as exc:
                                logger.error(
                                    "circuit_breaker.evaluate_error",
                                    error=str(exc),
                                )
                except Exception as exc:
                    logger.error(
                        "alert_engine.error",
                        error=str(exc),
                    )

    async def _fetch_position_record(
        self, position_id: str
    ) -> PositionRecord | None:
        """Lookup and materialize a PositionRecord by id for exit routing."""
        async with AsyncSessionLocal() as session:
            repo = PositionRepository(session)
            position_row = await repo.get_by_id(position_id)
            if position_row is None:
                return None
            return self._to_position_record(position_row)

    @staticmethod
    def _to_position_record(position: Position) -> PositionRecord:
        """Convert ORM Position row to immutable PositionRecord schema."""
        return PositionRecord(
            id=str(position.id),
            condition_id=str(position.condition_id),
            token_id=str(position.token_id),
            status=PositionStatus(str(position.status)),
            side=str(position.side),
            entry_price=Decimal(str(position.entry_price)),
            order_size_usdc=Decimal(str(position.order_size_usdc)),
            kelly_fraction=Decimal(str(position.kelly_fraction)),
            best_ask_at_entry=Decimal(str(position.best_ask_at_entry)),
            bankroll_usdc_at_entry=Decimal(str(position.bankroll_usdc_at_entry)),
            execution_action=ExecutionAction(str(position.execution_action)),
            reason=position.reason,
            routed_at_utc=position.routed_at_utc,
            recorded_at_utc=position.recorded_at_utc,
            realized_pnl=(
                Decimal(str(position.realized_pnl))
                if position.realized_pnl is not None
                else None
            ),
            exit_price=(
                Decimal(str(position.exit_price))
                if position.exit_price is not None
                else None
            ),
            closed_at_utc=position.closed_at_utc,
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

        if self._telegram_client is not None:
            await self._telegram_client.aclose()
            self._telegram_client = None

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
