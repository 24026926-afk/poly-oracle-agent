"""
src/schemas/ops.py

Typed deployment and operational validation schemas for WI-48.
Used by ``scripts/ops/check_deployment.py`` for bounded, auditable
deployment validation with mandatory dry-run enforcement.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ── Enums ──────────────────────────────────────────────────────────────────


class DeploymentFailureReason(str, Enum):
    """Typed reasons for deployment check failures."""

    DOCKER_NOT_INSTALLED = "docker_not_installed"
    COMPOSE_PLUGIN_NOT_INSTALLED = "compose_plugin_not_installed"
    SERVICE_NOT_RUNNING = "service_not_running"
    CONTAINER_RESTARTING = "container_restarting"
    ENV_FILE_ABSENT = "env_file_absent"
    DRY_RUN_MISSING = "dry_run_missing"
    DRY_RUN_FALSE = "dry_run_false"
    HEALTHZ_UNREACHABLE = "healthz_unreachable"
    READYZ_UNREACHABLE = "readyz_unreachable"
    READYZ_MALFORMED = "readyz_malformed"
    METRICS_UNREACHABLE = "metrics_unreachable"
    METRICS_FORBIDDEN_LABEL = "metrics_forbidden_label"
    METRICS_DISABLED = "metrics_disabled"
    SQLITE_MISSING = "sqlite_missing"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"


class DeploymentCheckStatus(str, Enum):
    """Outcome of a single deployment probe."""

    PASS = "pass"
    FAIL = "fail"
    SKIPPED = "skipped"


# ── Probe Results ──────────────────────────────────────────────────────────


class ComposeServiceStatus(BaseModel):
    """Snapshot of a single Docker Compose service."""

    model_config = {"frozen": True}

    service_name: str = Field(..., description="Compose service name")
    running: bool = Field(..., description="True if container state is 'running'")
    restart_count: int = Field(default=0, ge=0, description="Container restart count since start")
    exit_code: Optional[int] = Field(default=None, description="Last exit code if not running")


class HTTPProbeResult(BaseModel):
    """Result from probing an HTTP observability endpoint."""

    model_config = {"frozen": True}

    endpoint: str = Field(..., description="URL path probed (e.g. /healthz)")
    check: DeploymentCheckStatus
    status_code: Optional[int] = Field(default=None, description="HTTP status code returned")
    latency_ms: Optional[float] = Field(default=None, description="Round-trip latency in milliseconds")
    failure_reason: Optional[DeploymentFailureReason] = None
    detail: Optional[str] = Field(default=None, description="Human-readable detail for failures")


class DryRunGuardResult(BaseModel):
    """Result of DRY_RUN=true enforcement check."""

    model_config = {"frozen": True}

    check: DeploymentCheckStatus
    dry_run_present: bool = Field(..., description="True if DRY_RUN key exists in .env")
    dry_run_value: Optional[str] = Field(
        default=None,
        description="Redacted value indicator only (true/false/missing); never raw",
    )
    failure_reason: Optional[DeploymentFailureReason] = None
    detail: Optional[str] = None


class MetricsInspectionResult(BaseModel):
    """Result of Prometheus /metrics content validation."""

    model_config = {"frozen": True}

    check: DeploymentCheckStatus
    content_type_valid: bool = Field(default=False, description="True if Content-Type is text/plain")
    prometheus_format_valid: bool = Field(default=False, description="True if parseable Prometheus text")
    forbidden_labels_found: list[str] = Field(
        default_factory=list,
        description="List of forbidden label names detected in output",
    )
    failure_reason: Optional[DeploymentFailureReason] = None
    detail: Optional[str] = None


class DeploymentProbeResult(BaseModel):
    """Aggregated outcome for a single named deployment probe step."""

    model_config = {"frozen": True}

    probe_name: str = Field(..., description="Logical name of the probe step")
    status: DeploymentCheckStatus
    failure_reason: Optional[DeploymentFailureReason] = None
    detail: Optional[str] = None


class DeploymentValidationReport(BaseModel):
    """Top-level deployment validation report."""

    model_config = {"frozen": True}

    report_id: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        description="Unique report identifier (ISO 8601 UTC timestamp)",
    )
    overall_status: DeploymentCheckStatus = Field(
        ..., description="PASS only if all mandatory probes pass"
    )
    probes: list[DeploymentProbeResult] = Field(default_factory=list)
    exit_code: int = Field(default=0, description="Recommended process exit code (0=pass, non-zero=fail)")
    dry_run_verified: bool = Field(default=False, description="True if DRY_RUN=true was confirmed")
