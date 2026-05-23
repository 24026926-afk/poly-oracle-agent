"""
src/schemas/runtime_audit.py

WI-61 — Periodic Runtime Audit typed schemas.

Defines enums, probe results, summaries, and the terminal
``RuntimeAuditReport`` schema for the out-of-process runtime auditor.

All precision-sensitive numeric fields use ``Decimal`` and reject raw
``float`` at the Pydantic boundary.  All models are frozen where
appropriate.  No execution, signing, broadcasting, or Gatekeeper
schemas are referenced or modified.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


# ── Enums ─────────────────────────────────────────────────────────────────


class RuntimeAuditStatus(str, Enum):
    """Overall audit outcome."""

    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    SAFETY_GATE_FAILED = "SAFETY_GATE_FAILED"
    PROBE_ERROR = "PROBE_ERROR"


class RuntimeAuditExitCode(int, Enum):
    """Typed process exit codes.

    0 = healthy, 1 = degraded/warning, 2 = mandatory safety-gate failure,
    3 = config/probe error.
    """

    HEALTHY = 0
    DEGRADED = 1
    SAFETY_GATE_FAILED = 2
    PROBE_ERROR = 3


class RuntimeAuditFailureReason(str, Enum):
    """Typed reasons for audit failures or degraded findings."""

    DRY_RUN_FALSE = "DRY_RUN_FALSE"
    DRY_RUN_POSTURE_MISSING = "DRY_RUN_POSTURE_MISSING"
    FORBIDDEN_METRIC_LABEL = "FORBIDDEN_METRIC_LABEL"
    FORBIDDEN_ARTIFACT_CONTENT = "FORBIDDEN_ARTIFACT_CONTENT"
    HEALTH_PROBE_ERROR = "HEALTH_PROBE_ERROR"
    READINESS_PROBE_ERROR = "READINESS_PROBE_ERROR"
    METRICS_PROBE_ERROR = "METRICS_PROBE_ERROR"
    METRICS_PARSE_ERROR = "METRICS_PARSE_ERROR"
    DATABASE_PROBE_ERROR = "DATABASE_PROBE_ERROR"
    REPOSITORY_READ_ERROR = "REPOSITORY_READ_ERROR"
    ARTIFACT_WRITE_ERROR = "ARTIFACT_WRITE_ERROR"
    CONFIG_ERROR = "CONFIG_ERROR"
    TIMEOUT = "TIMEOUT"
    DOCKER_PROBE_ERROR = "DOCKER_PROBE_ERROR"
    LOG_TAIL_ERROR = "LOG_TAIL_ERROR"
    TELEGRAM_SEND_ERROR = "TELEGRAM_SEND_ERROR"
    REVIEWER_ERROR = "REVIEWER_ERROR"


class RuntimeAuditSeverity(str, Enum):
    """Finding severity levels."""

    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class RuntimeAuditFindingType(str, Enum):
    """Classification of audit findings."""

    SAFETY_GATE = "SAFETY_GATE"
    DEGRADATION = "DEGRADATION"
    WARNING = "WARNING"
    PROBE_ERROR = "PROBE_ERROR"
    UNAVAILABLE = "UNAVAILABLE"


class RuntimeAuditProbeStatus(str, Enum):
    """Status of an individual probe."""

    SUCCESS = "SUCCESS"
    DEGRADED = "DEGRADED"
    ERROR = "ERROR"
    UNAVAILABLE = "UNAVAILABLE"
    TIMEOUT = "TIMEOUT"


class RuntimeAuditLLMReviewStatus(str, Enum):
    """Status of the optional advisory LLM reviewer."""

    DISABLED = "DISABLED"
    SUCCESS = "SUCCESS"
    CONFIG_ERROR = "CONFIG_ERROR"
    TIMEOUT = "TIMEOUT"
    FORBIDDEN_CONTENT = "FORBIDDEN_CONTENT"
    HTTP_ERROR = "HTTP_ERROR"


# ── Decimal rejection helper ──────────────────────────────────────────────


def _reject_float(value: object) -> Decimal:
    """Reject raw float and coerce to Decimal."""
    if isinstance(value, float):
        raise ValueError("Float values are forbidden; use Decimal")
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception:
        raise ValueError(f"Cannot coerce to Decimal: {type(value).__name__}")


# ── Findings ──────────────────────────────────────────────────────────────


class RuntimeAuditFinding(BaseModel):
    """A single audit finding."""

    model_config = {"frozen": True}

    finding_type: RuntimeAuditFindingType
    severity: RuntimeAuditSeverity
    message: str = Field(..., max_length=1024)
    source: str = Field(..., max_length=128)
    failure_reason: Optional[RuntimeAuditFailureReason] = None
    detail: Optional[str] = Field(default=None, max_length=2048)


# ── Probe results ─────────────────────────────────────────────────────────


class RuntimeAuditProbeResult(BaseModel):
    """Generic probe result wrapper."""

    model_config = {"frozen": True}

    probe_name: str = Field(..., max_length=64)
    status: RuntimeAuditProbeStatus
    message: Optional[str] = Field(default=None, max_length=512)
    latency_ms: Optional[Decimal] = Field(default=None, ge=0)

    @field_validator("latency_ms", mode="before")
    @classmethod
    def _reject_float_latency(cls, v: object) -> object:
        if v is None:
            return v
        return _reject_float(v)


class RuntimeAuditHealthProbe(BaseModel):
    """Health endpoint probe result."""

    model_config = {"frozen": True}

    status: RuntimeAuditProbeStatus
    reachable: bool
    response_time_ms: Optional[Decimal] = Field(default=None, ge=0)
    message: Optional[str] = Field(default=None, max_length=512)

    @field_validator("response_time_ms", mode="before")
    @classmethod
    def _reject_float_rt(cls, v: object) -> object:
        if v is None:
            return v
        return _reject_float(v)


class RuntimeAuditDryRunPosture(BaseModel):
    """Evidence of dry-run mode from readiness probe."""

    model_config = {"frozen": True}

    dry_run_confirmed: bool
    source: str = Field(default="readyz", max_length=64)
    raw_value: Optional[str] = Field(default=None, max_length=32)


class RuntimeAuditReadinessProbe(BaseModel):
    """Readiness endpoint probe result."""

    model_config = {"frozen": True}

    status: RuntimeAuditProbeStatus
    reachable: bool
    ready: bool = False
    dry_run_posture: Optional[RuntimeAuditDryRunPosture] = None
    checks: dict[str, str] = Field(default_factory=dict)
    message: Optional[str] = Field(default=None, max_length=512)


class RuntimeAuditMetricSample(BaseModel):
    """A single parsed Prometheus metric sample."""

    model_config = {"frozen": True}

    name: str = Field(..., max_length=256)
    value: Decimal
    labels: dict[str, str] = Field(default_factory=dict)

    @field_validator("value", mode="before")
    @classmethod
    def _reject_float_value(cls, v: object) -> Decimal:
        return _reject_float(v)


class RuntimeAuditMetricsProbe(BaseModel):
    """Metrics endpoint probe result."""

    model_config = {"frozen": True}

    status: RuntimeAuditProbeStatus
    reachable: bool
    sample_count: int = Field(default=0, ge=0)
    forbidden_labels_found: list[str] = Field(default_factory=list)
    parse_error: Optional[str] = Field(default=None, max_length=512)
    message: Optional[str] = Field(default=None, max_length=512)


# ── Repository summaries ──────────────────────────────────────────────────


class RuntimeAuditLedgerSummary(BaseModel):
    """Bounded summary of the operational event ledger."""

    model_config = {"frozen": True}

    total_events: int = Field(default=0, ge=0)
    error_count: int = Field(default=0, ge=0)
    warning_count: int = Field(default=0, ge=0)
    ws_reconnect_count: int = Field(default=0, ge=0)
    budget_block_count: int = Field(default=0, ge=0)
    provider_failure_count: int = Field(default=0, ge=0)
    market_quarantine_count: int = Field(default=0, ge=0)
    readiness_change_count: int = Field(default=0, ge=0)
    alert_count: int = Field(default=0, ge=0)
    recovery_count: int = Field(default=0, ge=0)
    available: bool = True
    message: Optional[str] = Field(default=None, max_length=512)


class RuntimeAuditDecisionSummary(BaseModel):
    """Bounded summary of recent decisions."""

    model_config = {"frozen": True}

    total_decisions: int = Field(default=0, ge=0)
    buy_count: int = Field(default=0, ge=0)
    sell_count: int = Field(default=0, ge=0)
    hold_count: int = Field(default=0, ge=0)
    skip_count: int = Field(default=0, ge=0)
    available: bool = True
    message: Optional[str] = Field(default=None, max_length=512)


class RuntimeAuditMarketSummary(BaseModel):
    """Bounded summary of recent market snapshots."""

    model_config = {"frozen": True}

    total_snapshots: int = Field(default=0, ge=0)
    stale_count: int = Field(default=0, ge=0)
    freshness_seconds: Optional[Decimal] = Field(default=None, ge=0)
    available: bool = True
    message: Optional[str] = Field(default=None, max_length=512)

    @field_validator("freshness_seconds", mode="before")
    @classmethod
    def _reject_float_freshness(cls, v: object) -> object:
        if v is None:
            return v
        return _reject_float(v)


class RuntimeAuditPositionSummary(BaseModel):
    """Bounded summary of positions."""

    model_config = {"frozen": True}

    open_count: int = Field(default=0, ge=0)
    settled_count: int = Field(default=0, ge=0)
    total_open_exposure_usdc: Decimal = Field(default=Decimal("0"), ge=0)
    available: bool = True
    message: Optional[str] = Field(default=None, max_length=512)

    @field_validator("total_open_exposure_usdc", mode="before")
    @classmethod
    def _reject_float_exposure(cls, v: object) -> Decimal:
        return _reject_float(v)


class RuntimeAuditExecutionSummary(BaseModel):
    """Bounded summary of execution records."""

    model_config = {"frozen": True}

    total_executions: int = Field(default=0, ge=0)
    dry_run_count: int = Field(default=0, ge=0)
    live_count: int = Field(default=0, ge=0)
    available: bool = True
    message: Optional[str] = Field(default=None, max_length=512)


# ── Infrastructure probes ─────────────────────────────────────────────────


class RuntimeAuditDatabaseProbe(BaseModel):
    """SQLite database file probe result."""

    model_config = {"frozen": True}

    status: RuntimeAuditProbeStatus
    file_exists: bool
    file_size_bytes: int = Field(default=0, ge=0)
    growth_pct: Optional[Decimal] = Field(default=None)
    message: Optional[str] = Field(default=None, max_length=512)

    @field_validator("growth_pct", mode="before")
    @classmethod
    def _reject_float_growth(cls, v: object) -> object:
        if v is None:
            return v
        return _reject_float(v)


class RuntimeAuditDockerProbe(BaseModel):
    """Docker Compose status probe result."""

    model_config = {"frozen": True}

    status: RuntimeAuditProbeStatus
    docker_available: bool
    services_running: int = Field(default=0, ge=0)
    services_total: int = Field(default=0, ge=0)
    max_restart_count: int = Field(default=0, ge=0)
    message: Optional[str] = Field(default=None, max_length=512)


class RuntimeAuditLogTailSummary(BaseModel):
    """Bounded application log tail inspection result."""

    model_config = {"frozen": True}

    status: RuntimeAuditProbeStatus
    bytes_scanned: int = Field(default=0, ge=0)
    lines_scanned: int = Field(default=0, ge=0)
    forbidden_content_detected: bool = False
    error_count: int = Field(default=0, ge=0)
    warning_count: int = Field(default=0, ge=0)
    message: Optional[str] = Field(default=None, max_length=512)


# ── Artifact and alert results ────────────────────────────────────────────


class RuntimeAuditForbiddenContentCheck(BaseModel):
    """Result of a forbidden-content scan."""

    model_config = {"frozen": True}

    scanned: bool
    clean: bool
    forbidden_patterns_found: list[str] = Field(default_factory=list)


class RuntimeAuditArtifactWriteResult(BaseModel):
    """Result of writing audit artifacts."""

    model_config = {"frozen": True}

    success: bool
    json_path: Optional[str] = Field(default=None, max_length=512)
    md_path: Optional[str] = Field(default=None, max_length=512)
    latest_json_updated: bool = False
    latest_md_updated: bool = False
    failure_reason: Optional[str] = Field(default=None, max_length=512)
    forbidden_content_check: Optional[RuntimeAuditForbiddenContentCheck] = None


class RuntimeAuditTelegramAlert(BaseModel):
    """Configuration for a Telegram alert attempt."""

    model_config = {"frozen": True}

    enabled: bool
    exit_code: int
    message: Optional[str] = Field(default=None, max_length=1024)


class RuntimeAuditTelegramResult(BaseModel):
    """Result of a Telegram alert attempt."""

    model_config = {"frozen": True}

    sent: bool
    reason: Optional[str] = Field(default=None, max_length=256)
    failure_detail: Optional[str] = Field(default=None, max_length=512)


# ── LLM reviewer ─────────────────────────────────────────────────────────


class RuntimeAuditLLMReviewRequest(BaseModel):
    """Request to the optional advisory LLM reviewer."""

    model_config = {"frozen": True}

    audit_artifact_path: str = Field(..., max_length=512)
    model: str = Field(default="kimi-k2.6", max_length=64)
    timeout_seconds: Decimal = Field(default=Decimal("60"), ge=0)

    @field_validator("timeout_seconds", mode="before")
    @classmethod
    def _reject_float_timeout(cls, v: object) -> Decimal:
        return _reject_float(v)


class RuntimeAuditLLMReviewResult(BaseModel):
    """Result of the optional advisory LLM reviewer."""

    model_config = {"frozen": True}

    status: RuntimeAuditLLMReviewStatus
    review_path: Optional[str] = Field(default=None, max_length=512)
    model_used: Optional[str] = Field(default=None, max_length=64)
    failure_reason: Optional[str] = Field(default=None, max_length=512)
    forbidden_content_check: Optional[RuntimeAuditForbiddenContentCheck] = None


# ── Terminal report ───────────────────────────────────────────────────────


class RuntimeAuditReport(BaseModel):
    """Terminal audit report — the canonical output of the auditor."""

    model_config = {"frozen": True}

    status: RuntimeAuditStatus
    exit_code: RuntimeAuditExitCode
    generated_at_utc: datetime
    findings: list[RuntimeAuditFinding] = Field(default_factory=list)
    health_probe: Optional[RuntimeAuditHealthProbe] = None
    readiness_probe: Optional[RuntimeAuditReadinessProbe] = None
    metrics_probe: Optional[RuntimeAuditMetricsProbe] = None
    ledger_summary: Optional[RuntimeAuditLedgerSummary] = None
    decision_summary: Optional[RuntimeAuditDecisionSummary] = None
    market_summary: Optional[RuntimeAuditMarketSummary] = None
    position_summary: Optional[RuntimeAuditPositionSummary] = None
    execution_summary: Optional[RuntimeAuditExecutionSummary] = None
    database_probe: Optional[RuntimeAuditDatabaseProbe] = None
    docker_probe: Optional[RuntimeAuditDockerProbe] = None
    log_tail_summary: Optional[RuntimeAuditLogTailSummary] = None
    artifact_result: Optional[RuntimeAuditArtifactWriteResult] = None
    telegram_result: Optional[RuntimeAuditTelegramResult] = None
    llm_review_result: Optional[RuntimeAuditLLMReviewResult] = None
    forbidden_content_check: Optional[RuntimeAuditForbiddenContentCheck] = None

    @field_validator("generated_at_utc")
    @classmethod
    def _ensure_utc(cls, dt: datetime) -> datetime:
        if dt.tzinfo is None:
            raise ValueError("generated_at_utc must be timezone-aware (UTC)")
        return dt
