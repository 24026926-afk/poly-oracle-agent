"""
src/core/config.py

Central configuration manager for poly-oracle-agent using Pydantic Settings.
Loads values from environment variables or a `.env` file and enforces type safety.
"""

from pydantic import SecretStr, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppConfig(BaseSettings):
    """
    Application-wide configuration validated with Pydantic.
    """

    # --- 1. API Keys ---
    anthropic_api_key: SecretStr = Field(
        ..., 
        description="API Key for Anthropic Claude inference"
    )

    # --- 2. Web3 & Blockchain ---
    polygon_rpc_url: str = Field(
        ..., 
        description="RPC Endpoint for Polygon PoS node"
    )
    wallet_private_key: SecretStr = Field(
        ..., 
        description="EVM Wallet Private Key for signing transactions"
    )

    # --- 3. Database ---
    database_url: str = Field(
        default="sqlite+aiosqlite:///poly_oracle.db",
        description="Database connection string (SQLite by default via aiosqlite)"
    )

    # --- 4. Risk Parameters (from risk_management.md) ---
    kelly_fraction: float = Field(
        default=0.25, 
        description="Quarter-Kelly multiplier to scale f*"
    )
    min_confidence: float = Field(
        default=0.75, 
        description="Min LLM confidence score to allow execution"
    )
    max_spread_pct: float = Field(
        default=0.015, 
        description="Max bid-ask spread allowed (1.5%)"
    )
    max_exposure_pct: float = Field(
        default=0.03, 
        description="Max fraction of bankroll committed per trade (3%)"
    )
    min_ev_threshold: float = Field(
        default=0.02, 
        description="Min expected value required to execute (2% edge)"
    )
    min_ttr_hours: float = Field(
        default=4.0, 
        description="Min hours to resolution allowed for execution"
    )

    # Configuration for loading from .env
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )
