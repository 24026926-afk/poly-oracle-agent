"""
src/core/config.py

Central configuration manager for poly-oracle-agent using Pydantic Settings.
Loads values from environment variables or a ``.env`` file and enforces
type safety.  A module-level ``get_config()`` singleton ensures exactly one
``AppConfig`` instance is shared across all modules.
"""

import warnings
from typing import Any
from urllib.parse import urlparse
from decimal import Decimal
from functools import lru_cache

import structlog
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from web3 import Web3

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
        default="claude-3-5-sonnet-20241022",
        description="Anthropic model identifier",
    )
    anthropic_max_tokens: int = Field(
        default=4096, description="Max output tokens per Claude call"
    )
    anthropic_max_retries: int = Field(
        default=2, description="Max retries on malformed LLM responses"
    )

    # --- Polygon / Web3 ---
    polygon_rpc_url: str = Field(
        ..., description="RPC endpoint for Polygon PoS node"
    )
    wallet_address: str = Field(
        ..., description="Checksummed EIP-55 wallet address"
    )
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

    # --- Operational ---
    log_level: str = Field(
        default="INFO", description="Logging level: DEBUG, INFO, WARNING, ERROR"
    )
    dry_run: bool = Field(
        default=False,
        description="True = evaluate but never execute orders",
    )

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
            raise ValueError(
                f"log_level must be one of {_VALID_LOG_LEVELS}, got '{v}'"
            )
        return upper

    @field_validator("dry_run")
    @classmethod
    def _warn_dry_run(cls, v: bool) -> bool:
        if v:
            warnings.warn(
                "⚠️  DRY_RUN=True — orders will be evaluated but NEVER executed.",
                UserWarning,
                stacklevel=2,
            )
        return v

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
