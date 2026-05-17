"""
src/core/config.py

Central configuration manager for poly-oracle-agent using Pydantic Settings.
Loads values from environment variables or a ``.env`` file and enforces
type safety.  A module-level ``get_config()`` singleton ensures exactly one
``AppConfig`` instance is shared across all modules.
"""

import warnings
from typing import Any, Literal
from urllib.parse import urlparse
from decimal import Decimal
from functools import lru_cache

import structlog
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from web3 import Web3

from src.schemas.llm import LLMProvider

logger = structlog.get_logger(__name__)

_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR"}
_DRY_RUN_POLYGON_RPC_URL = "https://rpc.ankr.com/polygon"
_DRY_RUN_WALLET_ADDRESS = "0x1111111111111111111111111111111111111111"
_DRY_RUN_WALLET_PRIVATE_KEY = "0x" + "1" * 64


def _is_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _is_missing_secret(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, SecretStr):
        return value.get_secret_value().strip() == ""
    if isinstance(value, str):
        return value.strip() == ""
    return False


def _is_valid_http_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlparse(value.strip())
    return parsed.scheme in {"http", "https"} and parsed.netloc != ""


class AppConfig(BaseSettings):
    """Application-wide configuration validated with Pydantic."""

    # --- Anthropic ---
    anthropic_api_key: SecretStr = Field(
        ..., description="API key for Anthropic Claude inference"
    )
    anthropic_model: str = Field(
        default="claude-sonnet-4-20250514",
        description="Anthropic model identifier",
    )
    anthropic_max_tokens: int = Field(
        default=4096, description="Max output tokens per Claude call"
    )
    anthropic_max_retries: int = Field(
        default=2, description="Max retries on malformed LLM responses"
    )

    # --- Polygon / Web3 ---
    polygon_rpc_url: str = Field(..., description="RPC endpoint for Polygon PoS node")
    wallet_address: str = Field(..., description="Checksummed EIP-55 wallet address")
    wallet_private_key: SecretStr = Field(
        ..., description="EVM wallet private key for signing transactions"
    )

    # --- Polymarket CLOB ---
    clob_rest_url: str = Field(
        default="https://clob.polymarket.com",
        description="Polymarket CLOB REST API base URL",
    )
    clob_ws_url: str = Field(
        default="wss://ws-subscriptions-clob.polymarket.com/ws/market",
        description="Polymarket CLOB WebSocket endpoint",
    )
    gamma_api_url: str = Field(
        default="https://gamma-api.polymarket.com",
        description="Gamma API (market metadata) base URL",
    )

    # --- Risk parameters (match risk_management.md) ---
    kelly_fraction: float = Field(
        default=0.25, description="Quarter-Kelly multiplier to scale f*"
    )
    min_confidence: float = Field(
        default=0.75, description="Min LLM confidence score to allow execution"
    )
    max_spread_pct: float = Field(
        default=0.015, description="Max bid-ask spread allowed (1.5%)"
    )
    max_exposure_pct: float = Field(
        default=0.03,
        description="Max fraction of bankroll committed per trade (3%)",
    )
    enable_exposure_validator: bool = Field(
        default=False,
        description="Enable WI-30 global portfolio exposure validation gate",
    )
    enable_wallet_balance_check: bool = Field(
        default=False,
        description="Enable WI-31 live wallet balance validation gate",
    )
    max_category_exposure_pct: Decimal = Field(
        default=Decimal("0.015"),
        description=(
            "Per-category exposure cap as fraction of bankroll "
            "(defaults to half the global cap)"
        ),
    )
    min_matic_balance_wei: Decimal = Field(
        default=Decimal("100000000000000000"),
        description="Minimum wallet MATIC balance in WEI required for entry routing",
    )
    min_usdc_balance_usdc: Decimal = Field(
        default=Decimal("10"),
        description="Minimum wallet USDC balance required for entry routing",
    )
    min_ev_threshold: float = Field(
        default=0.02, description="Min expected value required to execute (2% edge)"
    )
    min_ttr_hours: float = Field(
        default=4.0, description="Min hours to resolution allowed for execution"
    )

    # --- Bankroll ---
    initial_bankroll_usdc: Decimal = Field(
        default=Decimal("1000"),
        description="Seed bankroll in USDC (override via INITIAL_BANKROLL_USDC env var)",
    )

    # --- Execution Router (WI-16) ---
    max_order_usdc: Decimal = Field(
        default=Decimal("50"),
        description="Hard cap on any single order in USDC",
    )
    max_slippage_tolerance: Decimal = Field(
        default=Decimal("0.02"),
        description="Max allowed deviation of best_ask above midpoint (2%)",
    )

    # --- Exit Strategy (WI-19) ---
    exit_position_max_age_hours: Decimal = Field(
        default=Decimal("48"),
        description="Max hours before an open position triggers time-decay exit",
    )
    exit_stop_loss_drop: Decimal = Field(
        default=Decimal("0.15"),
        description="Midpoint drop from entry that triggers stop-loss (0.15 = 15pp)",
    )
    exit_take_profit_gain: Decimal = Field(
        default=Decimal("0.20"),
        description="Midpoint gain from entry that triggers take-profit (0.20 = 20pp)",
    )
    exit_scan_interval_seconds: Decimal = Field(
        default=Decimal("60"),
        description="Seconds between periodic exit scans of open positions",
    )
    # --- Portfolio Aggregator (WI-23) ---
    enable_portfolio_aggregator: bool = Field(
        default=False,
        description="Enable periodic portfolio snapshot aggregation",
    )
    portfolio_aggregation_interval_sec: Decimal = Field(
        default=Decimal("30"),
        description="Seconds between periodic portfolio snapshot computations",
    )
    # --- Alert Engine (WI-25) ---
    alert_drawdown_usdc: Decimal = Field(
        default=Decimal("100"),
        description=(
            "USDC drawdown threshold for CRITICAL alert "
            "(fires when total_unrealized_pnl < -threshold)"
        ),
    )
    alert_stale_price_pct: Decimal = Field(
        default=Decimal("0.50"),
        description=(
            "Stale-price ratio threshold for WARNING alert "
            "(fires when stale/total > threshold)"
        ),
    )
    alert_max_open_positions: int = Field(
        default=20,
        description="Maximum open positions before WARNING alert fires",
    )
    alert_loss_rate_pct: Decimal = Field(
        default=Decimal("0.60"),
        description=(
            "Loss rate threshold for WARNING alert "
            "(fires when losing/settled > threshold)"
        ),
    )
    # --- Telegram Notifier (WI-26) ---
    enable_telegram_notifier: bool = Field(
        default=False,
        description="Enable Telegram notification delivery for alerts and execution events",
    )
    telegram_bot_token: SecretStr = Field(
        default=SecretStr(""),
        description="Telegram Bot API token (from @BotFather)",
    )
    telegram_chat_id: str = Field(
        default="",
        description="Telegram chat ID for notification delivery",
    )
    telegram_send_timeout_sec: Decimal = Field(
        default=Decimal("5"),
        description="Hard timeout in seconds for each Telegram sendMessage call",
    )
    # --- Circuit Breaker (WI-27) ---
    enable_circuit_breaker: bool = Field(
        default=False,
        description="Enable global circuit breaker to halt BUY routing on CRITICAL drawdown alerts",
    )
    circuit_breaker_override_closed: bool = Field(
        default=False,
        description="Force circuit breaker to CLOSED state on next evaluate_alerts() call (one-shot override)",
    )
    # --- Operational Alert Bridge (WI-50) ---
    enable_operational_alerts: bool = Field(
        default=False,
        description="Master enable for the operational alert bridge (Telegram operational alerts)",
    )
    enable_startup_alert: bool = Field(
        default=False,
        description="Send a process_started alert on orchestrator startup",
    )
    operational_readiness_degraded_threshold_sec: Decimal = Field(
        default=Decimal("300"),
        description="Seconds of sustained degraded readiness before an operational alert fires (default: 5 min)",
    )
    operational_websocket_stale_threshold_sec: Decimal = Field(
        default=Decimal("300"),
        description="Seconds of sustained WebSocket stale/disconnected before an operational alert fires (default: 5 min)",
    )
    operational_alert_cooldown_sec: Decimal = Field(
        default=Decimal("600"),
        description="Minimum seconds between duplicate operational alerts of the same type (default: 10 min)",
    )
    exit_min_bid_tolerance: Decimal = Field(
        default=Decimal("0.01"),
        description=(
            "Minimum acceptable best_bid for an exit SELL order. "
            "Orders below this threshold are rejected as degenerate exits."
        ),
    )

    # --- Gas ---
    max_gas_price_gwei: float = Field(
        default=500.0, description="Hard safety ceiling for gas price in Gwei"
    )
    fallback_gas_price_gwei: float = Field(
        default=50.0, description="Fallback gas price when RPC is unreachable"
    )
    gas_check_enabled: bool = Field(
        default=False,
        description="Enable WI-29 pre-evaluation gas viability gate",
    )
    dry_run_gas_price_wei: Decimal = Field(
        default=Decimal("30000000000"),
        description=(
            "Mock gas price in WEI used in dry-run mode "
            "and as deterministic fallback when needed"
        ),
    )
    gas_ev_buffer_pct: Decimal = Field(
        default=Decimal("0.10"),
        description="Required EV margin above gas cost (10% buffer by default)",
    )
    matic_usdc_price: Decimal = Field(
        default=Decimal("0.50"),
        description="Static MATIC/USDC fallback price for WI-29 price provider",
    )

    # --- Database ---
    database_url: str = Field(
        default="sqlite+aiosqlite:///./poly_oracle.db",
        description="Async database connection string",
    )

    # --- Grok Sentiment Oracle (WI-12) ---
    grok_api_key: SecretStr = Field(
        default=SecretStr(""),
        description="API key for Grok sentiment oracle",
    )
    grok_base_url: str = Field(
        default="https://api.x.ai/v1",
        description="Grok API base URL",
    )
    grok_model: str = Field(
        default="grok-3",
        description="Grok model identifier",
    )
    grok_mocked: bool = Field(
        default=True,
        description="Use deterministic mock sentiment responses (set False for live API)",
    )
    grok_live_enabled: bool = Field(
        default=False,
        description="Explicit gate to allow live xAI/Grok API calls (requires grok_mocked=False + valid API key)",
    )
    grok_timeout_seconds: float = Field(
        default=2.0,
        description="Timeout in seconds for live Grok API requests (both httpx and asyncio)",
    )
    grok_max_retries: int = Field(
        default=2,
        description="Max retries on transient Grok API failures (5xx, connection errors) before fallback",
    )

    # --- Operational ---
    log_level: str = Field(
        default="INFO", description="Logging level: DEBUG, INFO, WARNING, ERROR"
    )
    dry_run: bool = Field(
        default=False,
        description="True = evaluate but never execute orders",
    )

    # --- WI-46: WebSocket Reconnect & Health ---
    ws_reconnect_initial_backoff_seconds: Decimal = Field(
        default=Decimal("1.0"),
        description="Initial backoff in seconds before first WS reconnect attempt",
    )
    ws_reconnect_max_backoff_seconds: Decimal = Field(
        default=Decimal("60.0"),
        description="Maximum backoff ceiling in seconds for WS reconnect",
    )
    ws_reconnect_jitter_pct: Decimal = Field(
        default=Decimal("0.25"),
        description="Jitter range as fraction of current backoff (±jitter_pct * backoff)",
    )
    ws_pong_timeout_seconds: Decimal = Field(
        default=Decimal("30.0"),
        description="Seconds without PONG before treating connection as lost",
    )
    ws_consecutive_failure_degraded_threshold: int = Field(
        default=5,
        description="Consecutive WS failures before health state becomes DEGRADED",
    )
    health_server_port: int = Field(
        default=8080,
        description="Port for the local health HTTP server",
    )
    health_server_host: str = Field(
        default="127.0.0.1",
        description="Bind address for the local health HTTP server",
    )
    readiness_grace_window_seconds: Decimal = Field(
        default=Decimal("30.0"),
        description="Seconds after WS disconnect where /readyz may still return ready if DB is healthy",
    )
    enable_health_server: bool = Field(
        default=True,
        description="Start the local health HTTP server (/healthz, /readyz)",
    )

    # --- WI-47: Prometheus Metrics ---
    metrics_server_port: int = Field(
        default=8081,
        description="Port for the local Prometheus /metrics HTTP server",
    )
    metrics_server_host: str = Field(
        default="127.0.0.1",
        description="Bind address for the local Prometheus /metrics HTTP server",
    )
    enable_metrics_server: bool = Field(
        default=True,
        description="Start the local Prometheus /metrics HTTP server",
    )

    # --- WI-32: Concurrent Market Tracking ---
    max_active_markets: int = Field(
        default=10,
        description=(
            "Maximum number of markets activated at startup for parallel "
            "ingestion, context, evaluation, and execution.  Must be >= 1."
        ),
    )
    max_concurrent_markets: int = Field(
        default=15,
        description="Maximum number of markets tracked concurrently",
    )
    market_tracking_interval_sec: Decimal = Field(
        default=Decimal("10"),
        description="Cadence for market discovery refresh",
    )
    enable_market_tracking: bool = Field(
        default=True,
        description="Enable MarketTrackingTask in Orchestrator",
    )

    # --- WI-52: LLM Cost Guard and Cognitive Circuit Breaker ---
    enable_llm_cost_guard: bool = Field(
        default=True,
        description="Master enable for LLM budget enforcement before provider calls",
    )
    llm_hourly_call_limit: int = Field(
        default=60,
        ge=0,
        description="Max LLM provider calls per hour (0=no calls allowed)",
    )
    llm_daily_call_limit: int = Field(
        default=500,
        ge=0,
        description="Max LLM provider calls per day (0=no calls allowed)",
    )
    llm_daily_token_limit: int = Field(
        default=1000000,
        ge=0,
        description="Max total tokens consumed per day (0=no calls allowed)",
    )
    llm_daily_cost_limit_usd: Decimal = Field(
        default=Decimal("10"),
        ge=0,
        description="Max estimated LLM cost per day in USD (0=no calls allowed)",
    )
    llm_market_hourly_call_limit: int = Field(
        default=10,
        ge=0,
        description="Max calls per market per hour (0=no calls allowed)",
    )
    llm_repeated_hold_threshold: int = Field(
        default=5,
        ge=1,
        description="Consecutive HOLD outcomes before market cooldown",
    )
    llm_repeated_invalid_threshold: int = Field(
        default=3,
        ge=1,
        description="Consecutive invalid/malformed outputs before market cooldown",
    )
    llm_market_cooldown_seconds: Decimal = Field(
        default=Decimal("300"),
        ge=0,
        description="Seconds a market stays in cooldown after repeated failures",
    )
    llm_fallback_tokens_per_call: int = Field(
        default=4096,
        gt=0,
        description="Conservative fallback token count when provider usage is missing",
    )
    llm_cost_per_input_token_usd: Decimal = Field(
        default=Decimal("0.0000015"),
        ge=0,
        description="Estimated cost per input token in USD",
    )
    llm_cost_per_output_token_usd: Decimal = Field(
        default=Decimal("0.000006"),
        ge=0,
        description="Estimated cost per output token in USD",
    )

    # --- WI-54: Configurable DeepSeek Provider ---
    llm_provider: str = Field(
        default="anthropic",
        description="LLM evaluation provider: anthropic or deepseek",
    )
    deepseek_api_key: SecretStr = Field(
        default=SecretStr(""),
        description="API key for DeepSeek V4 Pro (required when llm_provider=deepseek)",
    )
    deepseek_base_url: str = Field(
        default="https://api.deepseek.com/anthropic",
        description="DeepSeek Anthropic-compatible endpoint base URL",
    )
    deepseek_model: str = Field(
        default="deepseek-chat",
        description="DeepSeek model identifier (deepseek-chat=V3, deepseek-reasoner=R1)",
    )
    deepseek_max_tokens: int = Field(
        default=4096,
        description="Max output tokens per DeepSeek call",
    )
    deepseek_max_retries: int = Field(
        default=2,
        description="Max retries on malformed DeepSeek responses",
    )

    # --- WI-53: Market Eligibility Preflight ---
    enable_market_discovery_preflight: bool = Field(
        default=False,
        description="Enable read-only preflight checks before market activation",
    )
    market_discovery_preflight_timeout_ms: Decimal = Field(
        default=Decimal("5000"),
        ge=0,
        description="Timeout in milliseconds for a single candidate preflight check",
    )
    market_discovery_max_preflight_candidates: int = Field(
        default=10,
        ge=1,
        description="Maximum number of candidates to evaluate per discovery cycle",
    )
    preflight_quarantine_duration_seconds: Decimal = Field(
        default=Decimal("300"),
        ge=0,
        description="Seconds a market stays in quarantine after repeated preflight failures",
    )
    preflight_max_spread_pct: Decimal = Field(
        default=Decimal("0.05"),
        ge=0,
        description="Maximum bid-ask spread allowed by preflight (5%)",
    )

    # --- WI-53: Market Evaluation Deduplication ---
    enable_market_evaluation_dedupe: bool = Field(
        default=False,
        description="Enable per-market deduplication of evaluation contexts",
    )
    dedupe_min_evaluation_interval_sec: Decimal = Field(
        default=Decimal("30"),
        ge=0,
        description="Minimum seconds between evaluations of the same market",
    )
    dedupe_midpoint_delta: Decimal = Field(
        default=Decimal("0.01"),
        ge=0,
        description="Minimum midpoint change to trigger a new evaluation (1pp)",
    )
    dedupe_spread_delta: Decimal = Field(
        default=Decimal("0.005"),
        ge=0,
        description="Minimum spread change to trigger a new evaluation (0.5pp)",
    )

    # --- WI-53: Prompt Queue Backpressure ---
    prompt_queue_maxsize: int = Field(
        default=50,
        ge=1,
        description="Maximum number of prompts allowed in the queue",
    )
    prompt_queue_coalesce_by_market: bool = Field(
        default=True,
        description="When queue is full, coalesce by market instead of dropping",
    )

    # --- WI-56: Operational Event Ledger ---
    enable_operational_event_ledger: bool = Field(
        default=False,
        description="Master enable for the operational event ledger (append-only audit trail)",
    )
    event_ledger_queue_size: int = Field(
        default=1000,
        ge=100,
        le=100000,
        description="Maximum capacity of the bounded event bus asyncio.Queue",
    )
    event_ledger_batch_size: int = Field(
        default=50,
        ge=1,
        le=500,
        description="Maximum number of events to flush in a single batch write",
    )
    event_ledger_flush_interval_sec: Decimal = Field(
        default=Decimal("10"),
        ge=Decimal("1"),
        le=Decimal("300"),
        description="Seconds between periodic batch flushes from the event bus",
    )
    event_ledger_shutdown_flush_timeout_sec: Decimal = Field(
        default=Decimal("30"),
        ge=Decimal("1"),
        le=Decimal("120"),
        description="Maximum seconds to wait for final flush during shutdown",
    )
    event_ledger_overflow_policy: Literal[
        "drop_oldest",
        "drop_newest",
        "drop_diagnostic",
    ] = Field(
        default="drop_oldest",
        description="Overflow policy: drop_oldest, drop_newest, or drop_diagnostic",
    )

    @field_validator(
        "llm_daily_cost_limit_usd",
        "llm_market_cooldown_seconds",
        "llm_cost_per_input_token_usd",
        "llm_cost_per_output_token_usd",
        mode="before",
    )
    @classmethod
    def _reject_float_llm_cost_fields(cls, value: Any) -> Decimal:
        if isinstance(value, float):
            raise ValueError("Float financial values are forbidden; use Decimal")
        if isinstance(value, Decimal):
            return value
        return Decimal(str(value))

    @model_validator(mode="after")
    def _validate_llm_provider_config(self) -> "AppConfig":
        """Validate WI-54 provider configuration at config-load time.

        - Unknown ``llm_provider`` → typed failure.
        - DeepSeek selected and key missing → typed failure.
        """
        raw_provider = self.llm_provider.strip().lower()
        known = {m.value for m in LLMProvider}
        if raw_provider not in known:
            raise ValueError(
                f"Unsupported llm_provider '{self.llm_provider}'. "
                f"Must be one of {sorted(known)}."
            )

        selected = LLMProvider(raw_provider)

        if selected == LLMProvider.DEEPSEEK:
            key = self.deepseek_api_key.get_secret_value().strip()
            if not key:
                raise ValueError(
                    "llm_provider=deepseek requires deepseek_api_key to be set."
                )

            from urllib.parse import urlparse

            parsed = urlparse(self.deepseek_base_url.strip())
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError(
                    f"deepseek_base_url is malformed: '{self.deepseek_base_url}'."
                )

        return self

    # --- Validators ---

    @model_validator(mode="before")
    @classmethod
    def _hydrate_dry_run_wallet_credentials(cls, data: Any) -> Any:
        if not isinstance(data, dict) or not _is_truthy(data.get("dry_run")):
            return data

        hydrated = dict(data)
        if not _is_valid_http_url(hydrated.get("polygon_rpc_url")):
            hydrated["polygon_rpc_url"] = _DRY_RUN_POLYGON_RPC_URL
        if _is_missing_secret(hydrated.get("wallet_address")):
            hydrated["wallet_address"] = _DRY_RUN_WALLET_ADDRESS
        if _is_missing_secret(hydrated.get("wallet_private_key")):
            hydrated["wallet_private_key"] = _DRY_RUN_WALLET_PRIVATE_KEY
        return hydrated

    @field_validator("wallet_address")
    @classmethod
    def _validate_wallet_address(cls, v: str) -> str:
        if not Web3.is_address(v):
            raise ValueError(f"Invalid EIP-55 address: {v}")
        return Web3.to_checksum_address(v)

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, v: str) -> str:
        upper = v.upper()
        if upper not in _VALID_LOG_LEVELS:
            raise ValueError(f"log_level must be one of {_VALID_LOG_LEVELS}, got '{v}'")
        return upper

    @field_validator("dry_run")
    @classmethod
    def _warn_dry_run(cls, v: bool) -> bool:
        if v:
            warnings.warn(
                "WARNING: DRY_RUN=True - orders will be evaluated but NEVER executed.",
                UserWarning,
                stacklevel=2,
            )
        return v

    @field_validator(
        "dry_run_gas_price_wei",
        "gas_ev_buffer_pct",
        "matic_usdc_price",
        "max_category_exposure_pct",
        "min_matic_balance_wei",
        "min_usdc_balance_usdc",
        "ws_reconnect_initial_backoff_seconds",
        "ws_reconnect_max_backoff_seconds",
        "ws_reconnect_jitter_pct",
        "ws_pong_timeout_seconds",
        "readiness_grace_window_seconds",
        "market_discovery_preflight_timeout_ms",
        "preflight_quarantine_duration_seconds",
        "preflight_max_spread_pct",
        "dedupe_min_evaluation_interval_sec",
        "dedupe_midpoint_delta",
        "dedupe_spread_delta",
        "event_ledger_flush_interval_sec",
        "event_ledger_shutdown_flush_timeout_sec",
        mode="before",
    )
    @classmethod
    def _reject_float_wi29_financial_fields(cls, value: Any) -> Decimal:
        if isinstance(value, float):
            raise ValueError("Float financial values are forbidden; use Decimal")
        if isinstance(value, Decimal):
            return value
        return Decimal(str(value))

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    """Return the singleton ``AppConfig`` instance.

    Uses ``lru_cache`` so env / .env is read exactly once, then shared
    across every module that imports this function.
    """
    return AppConfig()
