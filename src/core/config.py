"""
src/core/config.py

Central configuration manager for poly-oracle-agent using Pydantic Settings.
Loads values from environment variables or a ``.env`` file and enforces
type safety.  A module-level ``get_config()`` singleton ensures exactly one
``AppConfig`` instance is shared across all modules.
"""

import warnings
from decimal import Decimal
from functools import lru_cache

import structlog
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from web3 import Web3

logger = structlog.get_logger(__name__)

_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR"}


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

    # --- Operational ---
    log_level: str = Field(
        default="INFO", description="Logging level: DEBUG, INFO, WARNING, ERROR"
    )
    dry_run: bool = Field(
        default=False,
        description="True = evaluate but never execute orders",
    )

    # --- Validators ---

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
