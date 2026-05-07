"""
src/schemas/ops.py

Typed deployment and operational validation schemas for WI-48.
Used by ``scripts/ops/check_deployment.py`` for bounded, auditable
deployment validation with mandatory dry-run enforcement.
"""

from __future__ import annotations

import re
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


# ═══════════════════════════════════════════════════════════════════════════
# WI-50 — Telegram Operational Alert Bridge
# ═══════════════════════════════════════════════════════════════════════════

# ── Secret detection helpers ───────────────────────────────────────────────

# Patterns that indicate secret-like or high-cardinality content in alert
# reason or message fields. Must reject these at the Pydantic boundary.
_FORBIDDEN_PAYLOAD_PATTERNS: list[tuple[str, str]] = [
    # Private key hex (64 hex chars)
    ("private_key_hex", re.compile(r"\b[0-9a-fA-F]{64}\b")),
    # Private key with 0x prefix
    ("private_key_0x", re.compile(r"0x[0-9a-fA-F]{64}\b")),
    # Telegram bot token pattern (digits:alphanumeric)
    ("telegram_token", re.compile(r"\b\d{8,10}:[a-zA-Z0-9_-]{35,}\b")),
    # Condition ID (hex, typically 66 chars with 0x prefix)
    ("condition_id", re.compile(r"0x[0-9a-fA-F]{64}\b")),
    # Token/asset ID (digits, typically large)
    ("token_id", re.compile(r"\b\d{10,}\b")),
]

# Substrings that are banned from alert reason/message fields
_FORBIDDEN_SUBSTRINGS: list[str] = [
    "api_key",
    "api key",
    "secret",
    "private key",
    "private_key",
    "prompt_text",
    "reasoning",
    "wallet key",
    "passphrase",
]


def _scan_forbidden_payload(text: str) -> list[str]:
    """Scan text for secret-like patterns. Returns list of violation descriptions."""
    violations: list[str] = []
    for label, pattern in _FORBIDDEN_PAYLOAD_PATTERNS:
        if pattern.search(text):
            violations.append(f"forbidden_pattern:{label}")
    for substr in _FORBIDDEN_SUBSTRINGS:
        if substr.lower() in text.lower():
            violations.append(f"forbidden_substring:{substr}")
    return violations


# ── Enums ──────────────────────────────────────────────────────────────────


class OperationalAlertType(str, Enum):
    """Bounded set of operational alert types for the alert bridge."""

    PROCESS_STARTED = "process_started"
    READINESS_DEGRADED = "readiness_degraded"
    WEBSOCKET_STALE = "websocket_stale"
    CIRCUIT_BREAKER_OPENED = "circuit_breaker_opened"
    CIRCUIT_BREAKER_CLOSED = "circuit_breaker_closed"


class OperationalAlertSeverity(str, Enum):
    """Severity levels for operational alerts."""

    CRITICAL = "CRITICAL"
    WARNING = "WARNING"
    INFO = "INFO"


class OperationalAlertStatus(str, Enum):
    """Dispatch status for an operational alert."""

    DISPATCHED = "DISPATCHED"
    SUPPRESSED_COOLDOWN = "SUPPRESSED_COOLDOWN"
    SUPPRESSED_DISABLED = "SUPPRESSED_DISABLED"
    FAILED = "FAILED"


# ── Alert Schemas ──────────────────────────────────────────────────────────


class OperationalAlert(BaseModel):
    """A single typed operational alert payload.

    Payloads are secret-free and low-cardinality by construction.
    """

    model_config = {"frozen": True}

    alert_type: OperationalAlertType
    severity: OperationalAlertSeverity
    service_name: str = Field(default="poly-oracle-agent", min_length=1, max_length=64)
    first_seen_at_utc: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="UTC timestamp when the condition was first observed",
    )
    duration_seconds: Optional[float] = Field(
        default=None,
        ge=0,
        description="How long the condition has persisted, in seconds",
    )
    reason_code: str = Field(
        default="",
        max_length=128,
        description="Bounded, secret-free reason code (e.g. readiness_degraded, ws_pong_stale)",
    )
    message: str = Field(
        default="",
        max_length=512,
        description="Human-readable alert message; must be secret-free",
    )

    @field_validator("reason_code")
    @classmethod
    def _reject_forbidden_in_reason(cls, value: str) -> str:
        violations = _scan_forbidden_payload(value)
        if violations:
            raise ValueError(f"reason_code contains forbidden content: {violations}")
        return value

    @field_validator("message")
    @classmethod
    def _reject_forbidden_in_message(cls, value: str) -> str:
        violations = _scan_forbidden_payload(value)
        if violations:
            raise ValueError(f"message contains forbidden content: {violations}")
        return value


class OperationalAlertState(BaseModel):
    """Tracks the lifecycle of an operational alert for dedupe cooldown."""

    alert_type: OperationalAlertType
    first_seen_at_utc: Optional[datetime] = Field(
        default=None,
        description="When the alertable condition was first detected",
    )
    last_evaluated_at_utc: Optional[datetime] = Field(
        default=None,
        description="When the alert condition was last evaluated",
    )
    last_dispatched_at_utc: Optional[datetime] = Field(
        default=None,
        description="When the alert was last dispatched (sent to Telegram)",
    )
    is_active: bool = Field(
        default=False,
        description="True when the alertable condition is currently observed",
    )

    def is_within_cooldown(self, cooldown_seconds: float, now: Optional[datetime] = None) -> bool:
        """Return True if the last dispatch was within the cooldown window."""
        if self.last_dispatched_at_utc is None:
            return False
        now = now or datetime.now(timezone.utc)
        elapsed = (now - self.last_dispatched_at_utc).total_seconds()
        return elapsed < cooldown_seconds


class OperationalAlertConfig(BaseModel):
    """Configuration for operational alert thresholds and cooldowns."""

    model_config = {"frozen": True}

    enable_operational_alerts: bool = Field(
        default=False,
        description="Master enable for operational alert bridge",
    )
    enable_startup_alert: bool = Field(
        default=False,
        description="Send process_started alert on orchestrator startup",
    )
    readiness_degraded_threshold_seconds: float = Field(
        default=300.0,  # 5 minutes
        ge=0,
        description="Seconds of sustained degraded readiness before alerting",
    )
    websocket_stale_threshold_seconds: float = Field(
        default=300.0,  # 5 minutes
        ge=0,
        description="Seconds of sustained WebSocket stale/disconnected before alerting",
    )
    alert_cooldown_seconds: float = Field(
        default=600.0,  # 10 minutes
        ge=0,
        description="Minimum seconds between duplicate alerts of the same type",
    )


class OperationalAlertEvaluation(BaseModel):
    """Read-only result of evaluating alert conditions.

    No mutation methods — the evaluation is a pure decision record.
    """

    model_config = {"frozen": True}

    alert_type: OperationalAlertType
    should_dispatch: bool = Field(
        default=False,
        description="True if an alert should be sent",
    )
    suppressed_reason: Optional[str] = Field(
        default=None,
        description="Why dispatch was suppressed (cooldown, disabled, below threshold)",
    )
    alert: Optional[OperationalAlert] = Field(
        default=None,
        description="The alert payload if should_dispatch is True",
    )


class OperationalAlertDispatchResult(BaseModel):
    """Outcome of dispatching (or suppressing) an operational alert."""

    model_config = {"frozen": True}

    alert_type: OperationalAlertType
    status: OperationalAlertStatus
    alert: Optional[OperationalAlert] = None
    error_detail: Optional[str] = Field(
        default=None,
        description="Error detail if dispatch failed; must be secret-free",
    )

    @field_validator("error_detail")
    @classmethod
    def _reject_secrets_in_error(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        violations = _scan_forbidden_payload(value)
        if violations:
            raise ValueError(f"error_detail contains forbidden content: {violations}")
        return value
