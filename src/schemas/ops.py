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

from pydantic import BaseModel, Field, field_validator, model_validator


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


# ═══════════════════════════════════════════════════════════════════════════
# WI-49 — Secure Remote Operator Dashboard Access
# ═══════════════════════════════════════════════════════════════════════════


class DashboardAccessMode(str, Enum):
    """How the operator reaches the dashboard."""

    LOCAL = "local"
    SSH_TUNNEL = "ssh_tunnel"
    REVERSE_PROXY = "reverse_proxy"


class DashboardRuntimeConfig(BaseModel):
    """Profile-gated dashboard service runtime configuration."""

    model_config = {"frozen": True}

    profile_enabled: bool = Field(default=False, description="True when the dashboard Compose profile is active")
    service_name: str = Field(default="dashboard", description="Compose service name")
    bind_host: str = Field(
        default="127.0.0.1",
        description="Streamlit --server.address; 0.0.0.0 allowed in containers where port publishing enforces loopback restriction",
    )
    bind_port: int = Field(default=8501, ge=1, le=65535, description="Streamlit listen port")

    @field_validator("bind_host")
    @classmethod
    def _validate_bind_host(cls, value: str) -> str:
        """Reject empty or wildcard-IPv6 binds. 0.0.0.0 is allowed for container networking."""
        stripped = value.strip()
        if stripped in ("", "::"):
            raise ValueError("bind_host must be a concrete IP, not empty or IPv6 wildcard")
        return stripped


class DashboardDatabaseTarget(BaseModel):
    """Deployed SQLite database target for read-only dashboard access."""

    model_config = {"frozen": True}

    path: str = Field(default="/data/poly_oracle.db", min_length=1, description="Filesystem path to the SQLite database")
    read_only: bool = Field(default=True, description="Whether the connection must be read-only")
    access_mode: DashboardAccessMode = Field(default=DashboardAccessMode.LOCAL, description="Deployment access mode")

    @model_validator(mode="after")
    def _validate_deployed_read_only(self) -> "DashboardDatabaseTarget":
        """read_only must be True when access_mode is not LOCAL."""
        if self.access_mode != DashboardAccessMode.LOCAL and not self.read_only:
            raise ValueError("read_only must be True for non-LOCAL access modes")
        return self


class DashboardTunnelSpec(BaseModel):
    """SSH tunnel configuration for remote dashboard access."""

    model_config = {"frozen": True}

    local_port: int = Field(default=8501, ge=1, le=65535, description="Local port to bind on operator machine")
    remote_host: str = Field(default="127.0.0.1", min_length=1, description="Remote host (loopback on Droplet)")
    remote_port: int = Field(default=8501, ge=1, le=65535, description="Remote port Streamlit is listening on")
    ssh_user: str = Field(default="deploy", min_length=1, description="SSH user on the Droplet")
    droplet_ip: str = Field(default="", description="Droplet public IP (empty = must be configured)")

    @field_validator("remote_host")
    @classmethod
    def _validate_remote_host(cls, value: str) -> str:
        """Reject wildcard remote host binds."""
        stripped = value.strip()
        if stripped == "0.0.0.0":
            raise ValueError("remote_host must not be 0.0.0.0 (public exposure prohibited)")
        if stripped == "":
            raise ValueError("remote_host must not be empty")
        return stripped


class DashboardReadOnlyCheck(BaseModel):
    """Result of verifying read-only SQLite access for the dashboard."""

    model_config = {"frozen": True}

    passed: bool = Field(..., description="True if read-only connection was established without writes")
    reason: str = Field(default="", description="Human-readable explanation of the check result")
    write_attempted: bool = Field(default=False, description="True if a write was attempted and blocked")
    db_path: str = Field(default="", description="The database path that was checked")
    uri_mode: bool = Field(default=False, description="True if URI mode=ro was used for the connection")


class DashboardExposureCheck(BaseModel):
    """Result of scanning dashboard output for secrets and sensitive data."""

    model_config = {"frozen": True}

    passed: bool = Field(..., description="True if no prohibited patterns were found")
    prohibited_patterns: list[str] = Field(
        default_factory=list,
        description="List of prohibited pattern categories checked (e.g. 'private_key', 'api_key')",
    )
    violations_found: list[str] = Field(
        default_factory=list,
        description="Descriptions of any violations detected",
    )


class DashboardAccessValidationReport(BaseModel):
    """Aggregate validation report for dashboard access safety."""

    model_config = {"frozen": True}

    read_only_check: DashboardReadOnlyCheck = Field(..., description="Read-only SQLite enforcement result")
    exposure_check: DashboardExposureCheck = Field(..., description="Secret-free output scan result")
    tunnel_spec: Optional[DashboardTunnelSpec] = Field(default=None, description="SSH tunnel config if applicable")
    overall_pass: bool = Field(..., description="True only if all checks pass")
    summary: str = Field(default="", description="Human-readable summary of the validation outcome")
