"""
src/observability/health.py

Typed health schemas for WI-46 — 24/7 Connectivity Hardening.

Defines WebSocket connection state, health snapshots, readiness status,
reconnect configuration, and market lifecycle state types.
"""

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class WebSocketConnectionState(str, Enum):
    """Typed WebSocket connection lifecycle states."""

    CONNECTED = "CONNECTED"
    DISCONNECTED = "DISCONNECTED"
    RECONNECTING = "RECONNECTING"
    DEGRADED = "DEGRADED"


class MarketLifecycleState(str, Enum):
    """Typed market lifecycle states for explicit closed/inactive handling."""

    ACTIVE = "ACTIVE"
    CLOSED = "CLOSED"
    INACTIVE = "INACTIVE"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


class MarketClosedSkipReason(BaseModel):
    """Reason emitted when a market is skipped due to lifecycle state."""

    condition_id: str = Field(..., min_length=1)
    reason: MarketLifecycleState
    detail: str = Field(default="")


class WebSocketReconnectConfig(BaseModel):
    """Configuration for bounded exponential backoff with jitter."""

    initial_backoff_seconds: Decimal = Field(default=Decimal("1.0"), ge=0)
    max_backoff_seconds: Decimal = Field(default=Decimal("60.0"), ge=0)
    jitter_pct: Decimal = Field(default=Decimal("0.25"), ge=0, le=Decimal("1.0"))
    pong_timeout_seconds: Decimal = Field(default=Decimal("30.0"), ge=0)
    consecutive_failure_degraded_threshold: int = Field(default=5, ge=1)

    @field_validator(
        "initial_backoff_seconds",
        "max_backoff_seconds",
        "jitter_pct",
        "pong_timeout_seconds",
        mode="before",
    )
    @classmethod
    def _reject_float(cls, value: object) -> Decimal:
        if isinstance(value, float):
            raise ValueError("Float values are forbidden; use Decimal")
        if isinstance(value, Decimal):
            return value
        try:
            return Decimal(str(value))
        except Exception:
            raise ValueError(f"Cannot coerce to Decimal: {type(value).__name__}")


class WebSocketHealthSnapshot(BaseModel):
    """Typed health snapshot of the WebSocket connection.

    All timestamps are UTC. Counts are non-negative integers.
    """

    connection_state: WebSocketConnectionState = Field(
        default=WebSocketConnectionState.DISCONNECTED
    )
    last_connected_at_utc: Optional[datetime] = Field(default=None)
    last_heartbeat_sent_at_utc: Optional[datetime] = Field(default=None)
    last_pong_received_at_utc: Optional[datetime] = Field(default=None)
    reconnect_count: int = Field(default=0, ge=0)
    consecutive_failure_count: int = Field(default=0, ge=0)
    total_reconnect_count: int = Field(default=0, ge=0)
    last_error_reason: Optional[str] = Field(default=None)
    active_subscribed_asset_count: int = Field(default=0, ge=0)

    @field_validator("last_connected_at_utc", "last_heartbeat_sent_at_utc", "last_pong_received_at_utc")
    @classmethod
    def _ensure_utc(cls, dt: Optional[datetime]) -> Optional[datetime]:
        if dt is not None and dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt


class ReadinessStatus(str, Enum):
    """Readiness verdict for /readyz endpoint."""

    READY = "READY"
    DEGRADED = "DEGRADED"
    NOT_READY = "NOT_READY"


class HealthEndpointResponse(BaseModel):
    """Response body for health endpoints."""

    status: str
    timestamp_utc: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    checks: dict[str, str] = Field(default_factory=dict)

    @field_validator("timestamp_utc")
    @classmethod
    def _ensure_utc(cls, dt: datetime) -> datetime:
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt


class RuntimeHealthSnapshot(BaseModel):
    """Aggregate runtime health snapshot including WS and DB state."""

    ws_health: WebSocketHealthSnapshot = Field(default_factory=WebSocketHealthSnapshot)
    db_reachable: bool = Field(default=False)
    active_market_count: int = Field(default=0, ge=0)
    subscribed_asset_count: int = Field(default=0, ge=0)
