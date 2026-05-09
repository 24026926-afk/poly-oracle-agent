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
from src.agents.execution.exposure_validator import ExposureValidator
from src.agents.execution.exit_order_router import ExitOrderRouter
from src.agents.execution.exit_strategy_engine import ExitStrategyEngine
from src.agents.execution.gas_estimator import GasEstimator
from src.agents.execution.lifecycle_reporter import PositionLifecycleReporter
from src.agents.execution.matic_price_provider import MaticPriceProvider
from src.agents.execution.nonce_manager import NonceManager
from src.agents.execution.pnl_calculator import PnLCalculator
from src.agents.execution.portfolio_aggregator import PortfolioAggregator
from src.agents.execution.polymarket_client import PolymarketClient
from src.agents.execution.position_tracker import PositionTracker
from src.agents.execution.signer import TransactionSigner
from src.agents.execution.telegram_notifier import TelegramNotifier
from src.observability.operational_alerts import OperationalAlertBridge
from src.schemas.ops import OperationalAlertConfig
from src.agents.execution.wallet_balance_provider import WalletBalanceProvider
from src.agents.ingestion.market_discovery import MarketDiscoveryEngine
from src.agents.ingestion.rest_client import GammaRESTClient
from src.agents.ingestion.ws_client import CLOBWebSocketClient
from src.core.config import AppConfig, get_config
from src.observability.health import RuntimeHealthSnapshot
from src.observability.health_server import HealthServer
from src.observability.metrics import (
    DecisionLabel,
    DecisionMetricEvent,
    ExecutionMetricEvent,
    LatencyMetricEvent,
    MetricsRegistry,
)
from src.observability.metrics_server import MetricsServer
from src.db.models import Position
from src.db.engine import AsyncSessionLocal, engine
from src.db.repositories.position_repository import PositionRepository
from src.schemas.execution import (
    ExecutionAction,
    ExecutionResult,
    ExitOrderAction,
    PositionRecord,
)
from src.schemas.llm import MarketCategory
from src.schemas.market import MarketMetadata
from src.schemas.position import PositionStatus
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
        self.active_markets: list[MarketMetadata] = []
        self._condition_by_token: dict[str, str] = {}
        self._running = False

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
            self.w3,
            self.config.wallet_address,
            dry_run=self.config.dry_run,
        )
        self.gas_estimator = GasEstimator(config=self.config)
        if self.config.gas_check_enabled:
            self._gas_estimator: GasEstimator | None = self.gas_estimator
            self._matic_price_provider: MaticPriceProvider | None = MaticPriceProvider(
                config=self.config
            )
        else:
            self._gas_estimator = None
            self._matic_price_provider = None
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
        self._position_repo: PositionRepository | None = None
        if getattr(self.config, "enable_exposure_validator", False):
            self._exposure_validator: ExposureValidator | None = ExposureValidator(
                config=self.config,
            )
        else:
            self._exposure_validator = None
        if getattr(self.config, "enable_wallet_balance_check", False):
            self._wallet_balance_provider: WalletBalanceProvider | None = (
                WalletBalanceProvider(
                    config=self.config,
                    http_client=self._httpx_client,
                )
            )
        else:
            self._wallet_balance_provider = None
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

        # WI-50: Operational alert bridge
        self.operational_alert_bridge: OperationalAlertBridge | None = None
        self._op_alert_config = OperationalAlertConfig(
            enable_operational_alerts=self.config.enable_operational_alerts,
            enable_startup_alert=self.config.enable_startup_alert,
            readiness_degraded_threshold_seconds=float(
                self.config.operational_readiness_degraded_threshold_sec
            ),
            websocket_stale_threshold_seconds=float(
                self.config.operational_websocket_stale_threshold_sec
            ),
            alert_cooldown_seconds=float(
                self.config.operational_alert_cooldown_sec
            ),
        )
        if self._op_alert_config.enable_operational_alerts:
            self.operational_alert_bridge = OperationalAlertBridge(
                config=self._op_alert_config,
                notifier=self.telegram_notifier,
            )
            logger.info("operational_alerts.enabled")
        else:
            logger.info("operational_alerts.disabled")

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
        self._data_aggregator: DataAggregator | None = None
        self.gamma_client: GammaRESTClient | None = None
        self.discovery_engine: MarketDiscoveryEngine | None = None
        self.broadcaster: OrderBroadcaster | None = None
        self.market_tracking_task: asyncio.Task[Any] | None = None
        self._consecutive_spread_failures = 0

        # WI-46: Health server (started in start(), stopped in shutdown())
        self.health_server: HealthServer | None = None
        if self.config.enable_health_server:
            self.health_server = HealthServer(
                host=self.config.health_server_host,
                port=self.config.health_server_port,
                get_ws_health=self._get_runtime_health,
                check_db=self._check_db_reachable,
                readiness_grace_window_seconds=float(
                    self.config.readiness_grace_window_seconds
                ),
                dry_run=self.config.dry_run,
            )

        # WI-47: Metrics registry and server
        self.metrics_registry: MetricsRegistry = MetricsRegistry()
        self.metrics_server: MetricsServer | None = None
        if self.config.enable_metrics_server:
            self.metrics_server = MetricsServer(
                registry=self.metrics_registry,
                host=self.config.metrics_server_host,
                port=self.config.metrics_server_port,
            )

    async def start(self) -> None:
        """Start all layers and run until cancelled."""
        logger.info("orchestrator.starting")
        self._running = True

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

        # Create aggregator BEFORE activation so per-market registration runs.
        primary_cid = eligible[0].condition_id
        self.aggregator = DataAggregator(
            input_queue=self.market_queue,
            output_queue=self.prompt_queue,
            condition_id=primary_cid,
        )
        # WI-32: DataAggregator reference for concurrent tracking
        self._data_aggregator = self.aggregator

        # Activate up to max_concurrent_markets eligible markets
        max_active = min(len(eligible), self.config.max_concurrent_markets)
        markets_to_activate = eligible[:max_active]
        await self._activate_markets(markets_to_activate)

        logger.info(
            "activation_summary",
            discovered=len(eligible),
            eligible=len(eligible),
            activated=len(self.active_markets),
            active_condition_ids=[m.condition_id for m in self.active_markets],
        )
        # WI-32: DataAggregator reference for concurrent tracking
        self._data_aggregator = self.aggregator

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

        # WI-32: Optional concurrent market tracking task
        if self.config.enable_market_tracking:
            self.market_tracking_task = asyncio.create_task(
                self._market_tracking_loop(),
                name="MarketTrackingTask",
            )

        # WI-46: Start health server
        if self.health_server is not None:
            await self.health_server.start()

        # WI-47: Start metrics server and its sync loop together.
        # Gated by enable_metrics_server so test orchestrations can opt out
        # without inheriting a never-terminating background task in
        # asyncio.gather(*self._tasks).
        if self.metrics_server is not None:
            await self.metrics_server.start()
            self._tasks.append(
                asyncio.create_task(
                    self._metrics_sync_loop(),
                    name="MetricsSyncTask",
                )
            )

        # WI-50: Start operational alert evaluation loop
        if self.operational_alert_bridge is not None:
            self._tasks.append(
                asyncio.create_task(
                    self._operational_alert_loop(),
                    name="OperationalAlertTask",
                )
            )

        # WI-50: Send startup alert (one-shot, does not block)
        if self.operational_alert_bridge is not None:
            await self.operational_alert_bridge.dispatch_startup_alert()

        try:
            await asyncio.gather(*self._tasks)
        except asyncio.CancelledError:
            logger.info("orchestrator.cancelled")
            raise
        finally:
            await self.shutdown()

    def _set_aggregator_market_context(self, market: MarketMetadata) -> None:
        """Propagate Gamma market metadata into emitted evaluation state."""
        if self.aggregator is None:
            return
        self.aggregator._market_category = market.category
        self.aggregator._market_tags = list(market.tags)
        self.aggregator._market_question = market.question
        self.aggregator._yes_token_id = market.token_ids[0] if market.token_ids else None

    async def _activate_markets(self, markets: list[MarketMetadata]) -> None:
        """Activate multiple discovered Gamma markets for WS, aggregation, and routing.

        Accumulates all token IDs into a single subscription and builds a
        unified token→condition_id routing map so incoming WebSocket frames
        are routed to the correct market snapshot.

        WI-32 fixes:
        - Deduplicates markets by condition_id to avoid duplicate registration.
        - Deduplicates token IDs to avoid duplicate subscription payloads.
        - Fails closed on token collisions across markets.
        - Uses explicit ws_client.set_condition_by_token() setter.
        - Registers each market's metadata in the aggregator for per-market emit.
        """
        if not markets:
            logger.warning("orchestrator.activate_markets_empty")
            return

        # WI-32: Deduplicate markets by condition_id (first occurrence wins)
        seen_cids: set[str] = set()
        deduped: list[MarketMetadata] = []
        for market in markets:
            if market.condition_id not in seen_cids:
                seen_cids.add(market.condition_id)
                deduped.append(market)
        if len(deduped) < len(markets):
            logger.warning(
                "orchestrator.activate_markets_deduped",
                input_count=len(markets),
                unique_count=len(deduped),
            )

        all_token_ids_set: set[str] = set()
        condition_by_token: dict[str, str] = {}
        yes_token_by_token: dict[str, str] = {}

        for market in deduped:
            token_ids = list(market.token_ids)

            # WI-32: Detect token collisions across markets — fail closed.
            colliding = all_token_ids_set.intersection(token_ids)
            if colliding:
                logger.error(
                    "orchestrator.activate_markets_token_collision",
                    colliding_tokens=sorted(colliding),
                    market_condition_id=market.condition_id,
                )
                # Skip this market to avoid ambiguous routing.
                continue

            all_token_ids_set.update(token_ids)

            # token_ids[0] is the YES outcome token by Polymarket convention.
            yes_token = token_ids[0] if token_ids else None
            if yes_token:
                for token_id in token_ids:
                    condition_by_token[token_id] = market.condition_id
                    yes_token_by_token[token_id] = yes_token
                condition_by_token[market.condition_id] = market.condition_id
                yes_token_by_token[market.condition_id] = yes_token

        all_token_ids = sorted(all_token_ids_set)

        self.active_markets = list(deduped)
        self.active_condition_id = deduped[0].condition_id  # primary
        self._condition_by_token = condition_by_token

        self.ws_client.set_assets_ids(all_token_ids)
        self.ws_client.set_token_id_mapping(yes_token_by_token)
        self.ws_client.set_condition_by_token(condition_by_token)

        if self.aggregator is not None:
            self.aggregator.condition_id = self.active_condition_id
            self.aggregator.condition_by_token = condition_by_token
            # Clear stale per-market state before registering new markets
            self.aggregator.clear_markets()
            for market in deduped:
                yes_tok = market.token_ids[0] if market.token_ids else None
                self.aggregator.register_market(
                    condition_id=market.condition_id,
                    question=market.question,
                    category=market.category,
                    tags=list(market.tags),
                    yes_token_id=yes_tok,
                )
            # Reset primary market emit state
            self.aggregator.best_bid = 0.0
            self.aggregator.best_ask = 0.0
            self.aggregator._last_emit_time = 0.0
            self.aggregator._last_emitted_midpoint = None

        if getattr(self.ws_client, "_ws", None) is not None:
            await self.ws_client.subscribe_batch(all_token_ids)

        logger.info(
            "ws_subscribe_summary",
            activated_markets=len(self.active_markets),
            token_count=len(all_token_ids),
            unique_conditions=len(set(condition_by_token.values())),
        )

        for market in deduped:
            logger.info(
                "orchestrator.market_activated",
                condition_id=market.condition_id,
                category=market.category,
                token_count=len(market.token_ids),
            )

    async def _activate_market(self, market: MarketMetadata) -> None:
        """Activate a single discovered Gamma market (legacy, wraps _activate_markets)."""
        await self._activate_markets([market])

    def _select_rotation_candidate(
        self,
        eligible: list[MarketMetadata],
    ) -> MarketMetadata | None:
        """Rotate only after repeated spread failures on the active market."""
        if self.aggregator is None:
            return None

        best_bid = Decimal(str(self.aggregator.best_bid))
        best_ask = Decimal(str(self.aggregator.best_ask))
        if best_bid <= Decimal("0") or best_ask <= Decimal("0"):
            self._consecutive_spread_failures = 0
            return None

        spread_pct = (best_ask - best_bid) / best_ask
        max_spread_pct = Decimal(str(self.config.max_spread_pct))
        if spread_pct > max_spread_pct:
            self._consecutive_spread_failures += 1
        else:
            self._consecutive_spread_failures = 0

        if self._consecutive_spread_failures < 3 or len(eligible) <= 1:
            return None

        for market in eligible:
            if market.condition_id != self.active_condition_id:
                return market
        return None

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
                execution_result: ExecutionResult | None = None

                if (
                    execution_result is None
                    and getattr(self.config, "enable_exposure_validator", False)
                    and self._exposure_validator is not None
                ):
                    if self._position_repo is not None:
                        open_positions = await self._position_repo.get_open_positions()
                    else:
                        async with AsyncSessionLocal() as exposure_session:
                            exposure_repo = PositionRepository(exposure_session)
                            open_positions = await exposure_repo.get_open_positions()

                    proposed_size_raw = item.get("proposed_size_usdc", Decimal("0"))
                    proposed_size_usdc = Decimal(str(proposed_size_raw))

                    category_raw = item.get("category", MarketCategory.CULTURE)
                    if isinstance(category_raw, MarketCategory):
                        category = category_raw
                    else:
                        try:
                            category = MarketCategory(str(category_raw))
                        except Exception:
                            category = MarketCategory.CULTURE

                    bankroll_usdc = Decimal(
                        str(getattr(self.config, "initial_bankroll_usdc", Decimal("0")))
                    )
                    passed, summary = self._exposure_validator.validate_entry(
                        bankroll_usdc=bankroll_usdc,
                        proposed_size_usdc=proposed_size_usdc,
                        category=category,
                        open_positions=open_positions,
                    )
                    logger.info("exposure.summary_computed", **summary.model_dump())
                    if not passed:
                        logger.warning(
                            "exposure.limit_exceeded",
                            condition_id=condition_id,
                            aggregate_exposure_usdc=str(
                                summary.aggregate_exposure_usdc
                            ),
                            available_headroom_usdc=str(
                                summary.available_headroom_usdc
                            ),
                        )
                        execution_result = ExecutionResult(
                            action=ExecutionAction.SKIP,
                            reason="exposure_limit_exceeded",
                        )

                if (
                    execution_result is None
                    and getattr(self.config, "enable_wallet_balance_check", False)
                    and self._wallet_balance_provider is not None
                ):
                    balance_result = (
                        await self._wallet_balance_provider.check_balances()
                    )
                    logger.info(
                        "wallet.balance_checked",
                        matic_balance_wei=str(
                            getattr(balance_result, "matic_balance_wei", "")
                        ),
                        usdc_balance_usdc=str(
                            getattr(balance_result, "usdc_balance_usdc", "")
                        ),
                        matic_sufficient=getattr(
                            balance_result, "matic_sufficient", True
                        ),
                        usdc_sufficient=getattr(
                            balance_result, "usdc_sufficient", True
                        ),
                        check_passed=getattr(balance_result, "check_passed", True),
                        fallback_used=getattr(balance_result, "fallback_used", False),
                    )
                    if not getattr(balance_result, "check_passed", True):
                        logger.warning(
                            "wallet.balance_insufficient",
                            condition_id=condition_id,
                            matic_balance_wei=str(
                                getattr(balance_result, "matic_balance_wei", "")
                            ),
                            usdc_balance_usdc=str(
                                getattr(balance_result, "usdc_balance_usdc", "")
                            ),
                            matic_sufficient=getattr(
                                balance_result, "matic_sufficient", True
                            ),
                            usdc_sufficient=getattr(
                                balance_result, "usdc_sufficient", True
                            ),
                        )
                        execution_result = ExecutionResult(
                            action=ExecutionAction.SKIP,
                            reason="insufficient_wallet_balance",
                        )

                if (
                    execution_result is None
                    and self.config.gas_check_enabled
                    and self._gas_estimator is not None
                    and self._matic_price_provider is not None
                ):
                    expected_value_raw = item.get(
                        "expected_value_usdc",
                        getattr(eval_resp, "expected_value", None),
                    )
                    if expected_value_raw is not None:
                        expected_value_usdc = Decimal(str(expected_value_raw))
                        gas_price_wei = (
                            await self._gas_estimator.estimate_gas_price_wei()
                        )
                        matic_price = await self._matic_price_provider.get_matic_usdc()
                        gas_cost_usdc = self._gas_estimator.estimate_gas_cost_usdc(
                            gas_units=21000,
                            gas_price_wei=gas_price_wei,
                            matic_usdc_price=matic_price,
                        )
                        item["estimated_fee_usdc"] = gas_cost_usdc
                        if hasattr(eval_resp, "market_context"):
                            try:
                                setattr(
                                    eval_resp.market_context,
                                    "estimated_fee_usdc",
                                    gas_cost_usdc,
                                )
                            except Exception:
                                pass
                        if not self._gas_estimator.pre_evaluate_gas_check(
                            expected_value_usdc=expected_value_usdc,
                            gas_cost_usdc=gas_cost_usdc,
                        ):
                            logger.warning(
                                "gas.check_failed",
                                condition_id=condition_id,
                                expected_value_usdc=str(expected_value_usdc),
                                gas_cost_usdc=str(gas_cost_usdc),
                            )
                            execution_result = ExecutionResult(
                                action=ExecutionAction.SKIP,
                                reason="gas_cost_exceeds_ev",
                            )

                if (
                    execution_result is None
                    and self.circuit_breaker is not None
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
                elif execution_result is None:
                    execution_result = await self.execution_router.route(
                        response=eval_resp,
                        market_context=eval_resp.market_context,
                    )
                item["execution_result"] = execution_result

                yes_token_id = item.get("yes_token_id")
                if yes_token_id is None:
                    yes_token_id = getattr(
                        eval_resp.market_context, "yes_token_id", None
                    )
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

                # WI-47: Record decision and execution metrics
                try:
                    decision = eval_resp.recommended_action
                    if decision is not None:
                        decision_str = str(decision.value if hasattr(decision, 'value') else decision).upper()
                        if decision_str in ("BUY", "HOLD", "SKIP"):
                            await self.metrics_registry.record_decision(
                                DecisionMetricEvent(decision=DecisionLabel(decision_str))
                            )
                    await self.metrics_registry.record_execution(
                        ExecutionMetricEvent(action=execution_result.action)
                    )
                except Exception:
                    pass

                if self.telegram_notifier is not None and execution_result.action in (
                    ExecutionAction.EXECUTED,
                    ExecutionAction.DRY_RUN,
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
                next_market = self._select_rotation_candidate(eligible)
                if next_market is not None:
                    logger.info(
                        "orchestrator.market_rotation",
                        old_condition_id=self.active_condition_id,
                        new_condition_id=next_market.condition_id,
                        spread_failures=self._consecutive_spread_failures,
                    )
                    await self._activate_market(next_market)
                    self._consecutive_spread_failures = 0
                elif eligible:
                    # Refresh the full set of active markets on each discovery cycle
                    max_active = min(len(eligible), self.config.max_concurrent_markets)
                    markets_to_activate = eligible[:max_active]
                    await self._activate_markets(markets_to_activate)
                    logger.info(
                        "activation_summary",
                        discovered=len(eligible),
                        eligible=len(eligible),
                        activated=len(self.active_markets),
                        active_condition_ids=[m.condition_id for m in self.active_markets],
                    )
                else:
                    # WI-32: No eligible markets — deactivate all stale markets.
                    logger.warning(
                        "orchestrator.no_eligible_markets_on_refresh",
                        deactivating=len(self.active_markets),
                        was_condition_ids=[
                            m.condition_id for m in self.active_markets
                        ],
                    )
                    self.active_markets = []
                    self.active_condition_id = None
                    self._condition_by_token = {}
                    self.ws_client.set_assets_ids([])
                    self.ws_client.set_token_id_mapping({})
                    self.ws_client.set_condition_by_token({})
                    if self.aggregator is not None:
                        self.aggregator.clear_markets()
                        self.aggregator.condition_by_token = {}
            except Exception as exc:
                logger.error(
                    "orchestrator.discovery_loop_error",
                    error=str(exc),
                )

    async def _metrics_sync_loop(self) -> None:
        """Periodically sync WS health metrics into the Prometheus registry."""
        while True:
            await asyncio.sleep(15)  # sync every 15 seconds
            try:
                if self.ws_client is None:
                    continue
                ws_health = self.ws_client.get_health_snapshot()
                if ws_health is None:
                    continue
                from datetime import datetime, timezone

                heartbeat_age = Decimal("0")
                if ws_health.last_pong_received_at_utc is not None:
                    delta = (
                        datetime.now(timezone.utc)
                        - ws_health.last_pong_received_at_utc
                    )
                    heartbeat_age = Decimal(str(round(delta.total_seconds(), 3)))
                await self.metrics_registry.update_from_ws_health(
                    reconnect_count=ws_health.total_reconnect_count,
                    error_count=ws_health.consecutive_failure_count,
                    heartbeat_age_seconds=heartbeat_age,
                    active_asset_count=ws_health.active_subscribed_asset_count,
                )
            except Exception:
                pass

    async def _operational_alert_loop(self) -> None:
        """Periodically evaluate and dispatch operational alerts (WI-50).

        Non-blocking: alert dispatch is fire-and-forget and must not
        block ingestion, context, evaluation, execution, health, or metrics.
        """
        bridge = self.operational_alert_bridge
        if bridge is None:
            return

        while True:
            await asyncio.sleep(60)  # Evaluate every 60 seconds
            try:
                # Gather current state
                readiness_ready = False
                if self.health_server is not None:
                    health = await self._get_runtime_health()
                    readiness_ready = (
                        health.db_reachable
                        and health.ws_health is not None
                        and health.ws_health.connection_state.value == "CONNECTED"
                    )

                ws_health = (
                    self.ws_client.get_health_snapshot()
                    if self.ws_client is not None
                    else None
                )
                ws_connected = (
                    ws_health is not None
                    and ws_health.connection_state.value == "CONNECTED"
                )
                ws_pong_stale = False
                if ws_health is not None and ws_health.last_pong_received_at_utc is not None:
                    from datetime import datetime, timezone as tz

                    pong_age = (
                        datetime.now(tz.utc) - ws_health.last_pong_received_at_utc
                    ).total_seconds()
                    ws_pong_stale = pong_age > float(
                        self.config.ws_pong_timeout_seconds
                    )

                circuit_breaker_state = (
                    self.circuit_breaker.state
                    if self.circuit_breaker is not None
                    else None
                )

                await bridge.evaluate_and_dispatch_all(
                    readiness_ready=readiness_ready,
                    ws_connected=ws_connected,
                    ws_pong_stale=ws_pong_stale,
                    circuit_breaker_state=circuit_breaker_state,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("operational_alerts.evaluate_error")

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
                        exit_order_result.action
                        in (
                            ExitOrderAction.SELL_ROUTED,
                            ExitOrderAction.DRY_RUN,
                        )
                        and exit_order_result.exit_price is not None
                    ):
                        try:
                            gas_cost_usdc_for_settlement = Decimal("0")
                            if (
                                self._gas_estimator is not None
                                and self._matic_price_provider is not None
                            ):
                                gas_price_wei = (
                                    await self._gas_estimator.estimate_gas_price_wei()
                                )
                                matic_price = (
                                    await self._matic_price_provider.get_matic_usdc()
                                )
                                gas_cost_usdc_for_settlement = (
                                    self._gas_estimator.estimate_gas_cost_usdc(
                                        gas_units=21000,
                                        gas_price_wei=gas_price_wei,
                                        matic_usdc_price=matic_price,
                                    )
                                )
                            await self.pnl_calculator.settle(
                                position=position,
                                exit_price=exit_order_result.exit_price,
                                gas_cost_usdc=gas_cost_usdc_for_settlement,
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
            await asyncio.sleep(float(self.config.portfolio_aggregation_interval_sec))
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

    async def _fetch_position_record(self, position_id: str) -> PositionRecord | None:
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

    # ------------------------------------------------------------------
    # WI-32: Concurrent multi-market tracking
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_token_ids(snapshot: Any) -> list[str]:
        """Extract token_ids from a market discovery snapshot."""
        return getattr(snapshot, "token_ids", [])

    def _process_market_contexts(self, contexts: list[dict[str, Any]]) -> None:
        """Process market contexts returned from track_market().

        Contexts are already on prompt_queue via DataAggregator;
        this is a no-op hook for future extensions (logging, metrics).
        """
        pass

    async def _market_tracking_loop(self) -> None:
        """Concurrent multi-market tracking (WI-32).

        WI-32 fix: delegates to _activate_markets instead of using a separate
        aggregator.track_market() path.  This ensures both discovery loops
        converge on the same activation, subscription, and per-market state
        registration logic — no duplicate subscriptions or stale state.
        """
        while self._running:
            await asyncio.sleep(float(self.config.market_tracking_interval_sec))
            try:
                if self.discovery_engine is None:
                    logger.debug("market_tracking.discovery_not_ready")
                    continue

                snapshots = await self.discovery_engine.discover()
                if not snapshots:
                    # WI-32: No eligible markets — deactivate all stale markets
                    # (same clear path as _discovery_loop).
                    if self.active_markets:
                        logger.warning(
                            "market_tracking.no_markets_deactivating",
                            was_count=len(self.active_markets),
                            was_condition_ids=[
                                m.condition_id for m in self.active_markets
                            ],
                        )
                        self.active_markets = []
                        self.active_condition_id = None
                        self._condition_by_token = {}
                        self.ws_client.set_assets_ids([])
                        self.ws_client.set_token_id_mapping({})
                        self.ws_client.set_condition_by_token({})
                        if self.aggregator is not None:
                            self.aggregator.clear_markets()
                            self.aggregator.condition_by_token = {}
                    continue

                # Truncate to max_concurrent_markets
                if len(snapshots) > self.config.max_concurrent_markets:
                    logger.info(
                        "market_tracking.capped",
                        discovered=len(snapshots),
                        capped_to=self.config.max_concurrent_markets,
                    )
                    snapshots = snapshots[: self.config.max_concurrent_markets]

                # Delegate to the same activation path as _discovery_loop
                await self._activate_markets(snapshots)

                logger.info(
                    "market_tracking.completed",
                    activated=len(self.active_markets),
                    active_condition_ids=[m.condition_id for m in self.active_markets],
                )
            except Exception as exc:
                logger.error("market_tracking_loop.error", error=str(exc))
                # Fail-open: loop continues on next interval

    async def shutdown(self) -> None:
        """Stop running components, cancel tasks, and dispose shared resources."""
        logger.info("orchestrator.shutdown_start")

        # WI-32: Cancel MarketTrackingTask if running
        if (
            self.market_tracking_task is not None
            and not self.market_tracking_task.done()
        ):
            self.market_tracking_task.cancel()
            try:
                await self.market_tracking_task
            except asyncio.CancelledError:
                pass

        # WI-46: Stop health server
        if self.health_server is not None:
            await self.health_server.stop()

        # WI-47: Stop metrics server
        if self.metrics_server is not None:
            await self.metrics_server.stop()

        for stoppable in (self.aggregator, self.claude_client):
            if stoppable is None:
                continue
            try:
                await stoppable.stop()
            except Exception as exc:
                logger.warning(
                    "orchestrator.stop_failed",
                    component=type(stoppable).__name__,
                    error=str(exc),
                )

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

    # ------------------------------------------------------------------
    # WI-46: Health check callbacks for HealthServer
    # ------------------------------------------------------------------

    async def _get_runtime_health(self) -> RuntimeHealthSnapshot:
        """Return current runtime health snapshot for readiness checks."""
        ws_health = (
            self.ws_client.get_health_snapshot()
            if self.ws_client is not None
            else None
        )
        return RuntimeHealthSnapshot(
            ws_health=ws_health,
            db_reachable=await self._check_db_reachable(),
            active_market_count=len(self.active_markets) if self.active_markets else (1 if self.active_condition_id else 0),
            subscribed_asset_count=len(self.ws_client._assets_ids) if self.ws_client else 0,
        )

    @staticmethod
    async def _check_db_reachable() -> bool:
        """Check if the database is reachable."""
        try:
            async with AsyncSessionLocal() as session:
                await session.execute(
                    __import__("sqlalchemy").text("SELECT 1")
                )
            return True
        except Exception:
            return False


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
