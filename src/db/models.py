"""
SQLAlchemy ORM models for poly-oracle-agent.

Tables:
    - MarketSnapshot     : Point-in-time orderbook/price capture per market.
    - AgentDecisionLog   : Full LLM CoT audit trail, linked to a snapshot.
    - ExecutionTx        : On-chain transaction record, linked to a decision.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_uuid() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Declarative Base
# ---------------------------------------------------------------------------

class Base(DeclarativeBase):
    """Project-wide SQLAlchemy declarative base."""


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class DecisionAction(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class TxStatus(str, enum.Enum):
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"
    REVERTED = "REVERTED"


# ---------------------------------------------------------------------------
# Table 1: MarketSnapshot
# ---------------------------------------------------------------------------

class MarketSnapshot(Base):
    """
    Captures a point-in-time view of a Polymarket CLOB market.
    Written by the Market Ingestion Engine on every significant
    orderbook update received from the WebSocket stream.
    """

    __tablename__ = "market_snapshots"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_new_uuid
    )
    condition_id: Mapped[str] = mapped_column(
        String(128), nullable=False, index=True,
        comment="Polymarket condition ID (market identifier)"
    )
    question: Mapped[str] = mapped_column(
        Text, nullable=False,
        comment="Human-readable market question"
    )

    # --- Pricing ---
    best_bid: Mapped[float] = mapped_column(Float, nullable=False)
    best_ask: Mapped[float] = mapped_column(Float, nullable=False)
    last_trade_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    midpoint: Mapped[float] = mapped_column(Float, nullable=False)

    # --- Liquidity ---
    bid_liquidity_usdc: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    ask_liquidity_usdc: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    # --- Market Metadata ---
    outcome_token: Mapped[str] = mapped_column(
        String(64), nullable=False,
        comment="YES or NO token address being tracked"
    )
    market_end_date: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    volume_24h_usdc: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # --- Raw Payload ---
    raw_ws_payload: Mapped[str] = mapped_column(
        Text, nullable=False,
        comment="Original JSON string received from CLOB WebSocket"
    )

    # --- Timestamps ---
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, index=True
    )

    # --- Relationships ---
    decisions: Mapped[list["AgentDecisionLog"]] = relationship(
        "AgentDecisionLog", back_populates="snapshot", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_market_snapshots_condition_captured", "condition_id", "captured_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<MarketSnapshot condition={self.condition_id!r} "
            f"mid={self.midpoint:.4f} at={self.captured_at.isoformat()}>"
        )


# ---------------------------------------------------------------------------
# Table 2: AgentDecisionLog
# ---------------------------------------------------------------------------

class AgentDecisionLog(Base):
    """
    Full audit record of a single LLM evaluation cycle.
    Stores both the structured decision fields AND the raw Chain-of-Thought
    reasoning string returned by Claude for complete auditability.
    """

    __tablename__ = "agent_decision_logs"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_new_uuid
    )
    snapshot_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("market_snapshots.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # --- LLM Structured Output (mirrors LLMEvaluationResponse Pydantic schema) ---
    confidence_score: Mapped[float] = mapped_column(
        Float, nullable=False,
        comment="LLM confidence [0.0 – 1.0] in its probability estimate"
    )
    expected_value: Mapped[float] = mapped_column(
        Float, nullable=False,
        comment="Computed EV of the trade in USDC-equivalent basis"
    )
    decision_boolean: Mapped[bool] = mapped_column(
        Boolean, nullable=False,
        comment="True = execute trade; False = hold"
    )
    recommended_action: Mapped[DecisionAction] = mapped_column(
        SAEnum(DecisionAction, name="decision_action_enum"),
        nullable=False,
        default=DecisionAction.HOLD,
    )
    implied_probability: Mapped[float] = mapped_column(
        Float, nullable=False,
        comment="LLM's estimated true probability for the outcome"
    )

    # --- Full Audit Trail ---
    reasoning_log: Mapped[str] = mapped_column(
        Text, nullable=False,
        comment="Raw Chain-of-Thought text block returned verbatim by Claude"
    )
    prompt_version: Mapped[str] = mapped_column(
        String(32), nullable=False, default="v1.0.0",
        comment="Slug identifying the CoT prompt template version used"
    )
    llm_model_id: Mapped[str] = mapped_column(
        String(64), nullable=False,
        comment="Exact model string returned by Anthropic API, e.g. claude-3-5-sonnet-20241022"
    )
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # --- Timestamps ---
    evaluated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, index=True
    )

    # --- Relationships ---
    snapshot: Mapped["MarketSnapshot"] = relationship(
        "MarketSnapshot", back_populates="decisions"
    )
    execution: Mapped[Optional["ExecutionTx"]] = relationship(
        "ExecutionTx", back_populates="decision", uselist=False
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<AgentDecisionLog action={self.recommended_action.value} "
            f"ev={self.expected_value:.4f} conf={self.confidence_score:.2f} "
            f"execute={self.decision_boolean}>"
        )


# ---------------------------------------------------------------------------
# Table 3: ExecutionTx
# ---------------------------------------------------------------------------

class ExecutionTx(Base):
    """
    Records every on-chain transaction attempt initiated by the
    Web3 Execution Node, whether confirmed, failed, or reverted.
    One-to-one with AgentDecisionLog (a decision triggers at most one tx).
    """

    __tablename__ = "execution_txs"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_new_uuid
    )
    decision_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agent_decision_logs.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,  # Enforces 1-to-1 with AgentDecisionLog
        index=True,
    )

    # --- Transaction Identity ---
    tx_hash: Mapped[Optional[str]] = mapped_column(
        String(66), nullable=True, unique=True,
        comment="0x-prefixed Polygon transaction hash"
    )
    status: Mapped[TxStatus] = mapped_column(
        SAEnum(TxStatus, name="tx_status_enum"),
        nullable=False,
        default=TxStatus.PENDING,
        index=True,
    )

    # --- Order Details ---
    side: Mapped[str] = mapped_column(
        String(4), nullable=False,
        comment="BUY or SELL"
    )
    size_usdc: Mapped[float] = mapped_column(
        Float, nullable=False,
        comment="USDC amount committed to this order"
    )
    limit_price: Mapped[float] = mapped_column(
        Float, nullable=False,
        comment="Limit price submitted to the CLOB"
    )
    condition_id: Mapped[str] = mapped_column(
        String(128), nullable=False, index=True
    )
    outcome_token: Mapped[str] = mapped_column(String(64), nullable=False)

    # --- Gas Accounting ---
    gas_limit: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    gas_price_gwei: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    gas_used: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    nonce: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # --- Receipt ---
    block_number: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
        comment="Revert reason or RPC error string if tx failed"
    )

    # --- Timestamps ---
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # --- Relationships ---
    decision: Mapped["AgentDecisionLog"] = relationship(
        "AgentDecisionLog", back_populates="execution"
    )

    __table_args__ = (
        Index("ix_execution_txs_status_submitted", "status", "submitted_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<ExecutionTx hash={self.tx_hash!r} "
            f"status={self.status.value} size={self.size_usdc:.2f} USDC>"
        )


# ---------------------------------------------------------------------------
# Table 4: Position
# ---------------------------------------------------------------------------

class Position(Base):
    """Execution-time position tracking snapshot (WI-17)."""

    __tablename__ = "positions"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_new_uuid
    )
    condition_id: Mapped[str] = mapped_column(String(256), nullable=False)
    token_id: Mapped[str] = mapped_column(String(256), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)

    entry_price: Mapped[Decimal] = mapped_column(
        Numeric(precision=38, scale=18),
        nullable=False,
    )
    order_size_usdc: Mapped[Decimal] = mapped_column(
        Numeric(precision=38, scale=18),
        nullable=False,
    )
    kelly_fraction: Mapped[Decimal] = mapped_column(
        Numeric(precision=38, scale=18),
        nullable=False,
    )
    best_ask_at_entry: Mapped[Decimal] = mapped_column(
        Numeric(precision=38, scale=18),
        nullable=False,
    )
    bankroll_usdc_at_entry: Mapped[Decimal] = mapped_column(
        Numeric(precision=38, scale=18),
        nullable=False,
    )

    execution_action: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    routed_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recorded_at_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
    )

    __table_args__ = (
        Index("ix_positions_condition_id", "condition_id"),
        Index("ix_positions_status", "status"),
        Index("ix_positions_condition_id_status", "condition_id", "status"),
    )
