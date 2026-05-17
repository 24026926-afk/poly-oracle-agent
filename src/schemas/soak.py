"""
src/schemas/soak.py

Typed paper-trading soak evidence schemas for WI-51.
Used by ``scripts/ops/collect_soak_evidence.py`` for auditable,
secret-free soak-test evidence collection.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field


# ── Enums ──────────────────────────────────────────────────────────────────


class SoakVerdict(str, Enum):
    """Top-level verdict for a paper-trading soak test."""

    PASS = "pass"
    FAIL = "fail"
    INCOMPLETE = "incomplete"


class SoakProbeStatus(str, Enum):
    """Outcome of a single soak evidence probe."""

    PASS = "pass"
    FAIL = "fail"
    SKIPPED = "skipped"
    INCOMPLETE = "incomplete"
    NOT_APPLICABLE = "not_applicable"


class SoakFailureReason(str, Enum):
    """Bounded reason codes for failed or incomplete soak probes."""

    DRY_RUN_FALSE = "dry_run_false"
    DRY_RUN_MISSING = "dry_run_missing"
    RUNTIME_DRY_RUN_UNCONFIRMED = "runtime_dry_run_unconfirmed"
    DURATION_TOO_SHORT = "duration_too_short"
    TIMEOUT = "timeout"
    SERVICE_NOT_RUNNING = "service_not_running"
    RESTART_COUNT_UNAVAILABLE = "restart_count_unavailable"
    PARSE_ERROR = "parse_error"
    HEALTHZ_UNREACHABLE = "healthz_unreachable"
    READYZ_NOT_READY = "readyz_not_ready"
    READYZ_DEGRADED = "readyz_degraded"
    READYZ_UNREACHABLE = "readyz_unreachable"
    READYZ_UNKNOWN_STATUS = "readyz_unknown_status"
    METRICS_UNREACHABLE = "metrics_unreachable"
    METRICS_INVALID = "metrics_invalid"
    SQLITE_MISSING = "sqlite_missing"
    SQLITE_LOCKED = "sqlite_locked"
    SQLITE_ERROR = "sqlite_error"
    NO_PERSISTENCE_ACTIVITY = "no_persistence_activity"
    NO_AGENT_ACTIVITY = "no_agent_activity"
    INVALID_RECOVERY_METHOD = "invalid_recovery_method"
    RECOVERY_NOT_VERIFIED = "recovery_not_verified"


class SoakReadinessStatus(str, Enum):
    """Runtime /readyz status values represented in soak evidence."""

    READY = "READY"
    DEGRADED = "DEGRADED"
    NOT_READY = "NOT_READY"
    MALFORMED = "MALFORMED"
    UNKNOWN = "UNKNOWN"


class SoakRecoveryMethod(str, Enum):
    """Allowed recovery methods for soak evidence."""

    DOCKER_COMPOSE_RESTART = "docker compose restart"
    HOST_REBOOT = "host reboot"


# ── Probe Results ──────────────────────────────────────────────────────────


class SoakProbeResult(BaseModel):
    """Generic soak evidence probe result."""

    model_config = {"frozen": True}

    probe_name: str = Field(..., min_length=1, description="Logical name of the probe")
    status: SoakProbeStatus
    detail: str = Field(default="", description="Human-readable detail")
    failure_reason: Optional[SoakFailureReason] = Field(
        default=None,
        description="Bounded reason code on failure",
    )


class SoakServiceStatus(BaseModel):
    """Snapshot of Compose service status for soak evidence."""

    model_config = {"frozen": True}

    service_name: str = Field(default="orchestrator", min_length=1)
    running: bool = Field(default=False)
    restart_count: int = Field(default=0, ge=0)
    exit_code: Optional[int] = Field(default=None)


class SoakHealthEvidence(BaseModel):
    """Health/readiness endpoint evidence."""

    model_config = {"frozen": True}

    healthz_reachable: bool = Field(default=False)
    healthz_status_code: Optional[int] = Field(default=None)
    readyz_reachable: bool = Field(default=False)
    readyz_status_code: Optional[int] = Field(default=None)
    readyz_status: Optional[SoakReadinessStatus] = Field(
        default=None,
        description="READY | DEGRADED | NOT_READY",
    )
    degraded_reason: Optional[str] = Field(
        default=None, description="Why readiness is degraded, if applicable"
    )
    health_probe: SoakProbeResult = Field(
        ..., description="Aggregated health probe result"
    )


class SoakMetricsEvidence(BaseModel):
    """Metrics endpoint evidence."""

    model_config = {"frozen": True}

    metrics_reachable: bool = Field(default=False)
    metrics_status_code: Optional[int] = Field(default=None)
    prometheus_format_valid: bool = Field(default=False)
    metrics_probe: SoakProbeResult = Field(
        ..., description="Aggregated metrics probe result"
    )


class SoakDatabaseEvidence(BaseModel):
    """SQLite persistence evidence."""

    model_config = {"frozen": True}

    db_file_exists: bool = Field(default=False)
    db_file_path: str = Field(default="/data/poly_oracle.db")
    db_file_size_bytes: int = Field(default=0, ge=0)
    db_growth_bytes: int = Field(default=0, description="Bytes grown since soak start")
    db_grew: bool = Field(
        default=False, description="True if file size increased during soak"
    )
    recent_decision_count: int = Field(default=0, ge=0)
    recent_snapshot_count: int = Field(default=0, ge=0)
    sqlite_locked: bool = Field(default=False)
    db_probe: SoakProbeResult = Field(
        ..., description="Aggregated database probe result"
    )


class SoakRecoveryEvidence(BaseModel):
    """Host/container restart recovery evidence."""

    model_config = {"frozen": True}

    recovery_tested: bool = Field(
        default=False, description="True if reboot/restart recovery was tested"
    )
    recovery_method: Optional[SoakRecoveryMethod] = Field(
        default=None,
        description="docker compose restart | host reboot",
    )
    service_recovered: Optional[bool] = Field(
        default=None, description="True if service came back healthy after restart"
    )
    recovery_probe: SoakProbeResult = Field(
        ..., description="Aggregated recovery probe result"
    )


# ── Top-Level Report ───────────────────────────────────────────────────────


class SoakEvidenceReport(BaseModel):
    """Top-level paper-trading soak evidence report.

    This is an audit artifact — it cannot authorize live trading.
    """

    model_config = {"frozen": True}

    report_id: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).strftime(
            "soak-%Y%m%dT%H%M%SZ"
        ),
    )
    target_host: str = Field(
        default="localhost",
        description="Host where evidence was collected; localhost or explicit host without private IPs",
    )
    soak_start_utc: Optional[datetime] = Field(
        default=None, description="Operator-provided soak start timestamp"
    )
    soak_end_utc: Optional[datetime] = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When evidence was collected",
    )
    duration_hours: float = Field(default=0.0, ge=0.0)
    dry_run_confirmed: bool = Field(default=False)
    verdict: SoakVerdict = Field(default=SoakVerdict.INCOMPLETE)
    verdict_reason: str = Field(default="", description="Why the verdict was assigned")
    service_status: Optional[SoakServiceStatus] = Field(default=None)
    health_evidence: Optional[SoakHealthEvidence] = Field(default=None)
    metrics_evidence: Optional[SoakMetricsEvidence] = Field(default=None)
    database_evidence: Optional[SoakDatabaseEvidence] = Field(default=None)
    recovery_evidence: Optional[SoakRecoveryEvidence] = Field(default=None)
    telegram_enabled: Optional[bool] = Field(default=None)
    telegram_status: Optional[SoakProbeStatus] = Field(default=None)
    probes: list[SoakProbeResult] = Field(default_factory=list)
    exit_code: int = Field(
        default=0, description="Recommended process exit code (0=pass, non-zero=fail)"
    )
    live_trading_authorized: Literal[False] = Field(
        default=False,
        description="ALWAYS False — soak evidence cannot authorize live trading",
    )
