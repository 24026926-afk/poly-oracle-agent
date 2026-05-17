"""
src/schemas/ops.py

Typed deployment and operational validation schemas for WI-48.
Used by ``scripts/ops/check_deployment.py`` for bounded, auditable
deployment validation with mandatory dry-run enforcement.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Literal, Optional

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


# ═══════════════════════════════════════════════════════════════════════════
# WI-56 — Operational Event Ledger
# ═══════════════════════════════════════════════════════════════════════════


# ── Enums ──────────────────────────────────────────────────────────────────


class OperationalEventType(str, Enum):
    """Stable, bounded set of operational event types for the audit ledger."""

    START = "START"
    SHUTDOWN = "SHUTDOWN"
    CONFIG_LOADED = "CONFIG_LOADED"
    MARKET_DISCOVERED = "MARKET_DISCOVERED"
    MARKET_REJECTED = "MARKET_REJECTED"
    MARKET_QUARANTINE = "MARKET_QUARANTINE"
    WS_CONNECTED = "WS_CONNECTED"
    WS_RECONNECT = "WS_RECONNECT"
    WS_PONG_STALE = "WS_PONG_STALE"
    READY_STATE_CHANGED = "READY_STATE_CHANGED"
    LLM_CALL_STARTED = "LLM_CALL_STARTED"
    LLM_CALL_BLOCKED = "LLM_CALL_BLOCKED"
    BUDGET_BLOCK = "BUDGET_BLOCK"
    COOLDOWN_BLOCK = "COOLDOWN_BLOCK"
    PROVIDER_FAILURE = "PROVIDER_FAILURE"
    DECISION_ACCEPTED = "DECISION_ACCEPTED"
    DECISION_SKIPPED = "DECISION_SKIPPED"
    EXECUTION_DRY_RUN = "EXECUTION_DRY_RUN"
    CIRCUIT_BREAKER_OPEN = "CIRCUIT_BREAKER_OPEN"
    CIRCUIT_BREAKER_CLOSED = "CIRCUIT_BREAKER_CLOSED"
    ALERT_SENT = "ALERT_SENT"
    ERROR_RECOVERED = "ERROR_RECOVERED"


class OperationalEventSeverity(str, Enum):
    """Severity levels for operational audit events."""

    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"
    ERROR = "ERROR"


class OperationalEventSource(str, Enum):
    """Stable source-component identifiers for operational events."""

    ORCHESTRATOR = "ORCHESTRATOR"
    INGESTION = "INGESTION"
    CONTEXT = "CONTEXT"
    EVALUATION = "EVALUATION"
    EXECUTION = "EXECUTION"
    OBSERVABILITY = "OBSERVABILITY"
    DATABASE = "DATABASE"


class OperationalEventReasonCode(str, Enum):
    """Stable reason codes for operational event outcomes."""

    # Lifecycle
    STARTUP = "STARTUP"
    GRACEFUL_SHUTDOWN = "GRACEFUL_SHUTDOWN"
    FORCED_SHUTDOWN = "FORCED_SHUTDOWN"
    # Config
    CONFIG_VALID = "CONFIG_VALID"
    CONFIG_INVALID = "CONFIG_INVALID"
    # Market discovery
    MARKET_FOUND = "MARKET_FOUND"
    MARKET_NOT_FOUND = "MARKET_NOT_FOUND"
    MARKET_ELIGIBLE = "MARKET_ELIGIBLE"
    MARKET_INELIGIBLE = "MARKET_INELIGIBLE"
    MARKET_QUARANTINED = "MARKET_QUARANTINED"
    MARKET_COOLDOWN = "MARKET_COOLDOWN"
    # WebSocket
    WS_ESTABLISHED = "WS_ESTABLISHED"
    WS_LOST = "WS_LOST"
    WS_RECONNECTED = "WS_RECONNECTED"
    WS_PONG_TIMEOUT = "WS_PONG_TIMEOUT"
    WS_ERROR = "WS_ERROR"
    # Readiness
    READY = "READY"
    DEGRADED = "DEGRADED"
    NOT_READY = "NOT_READY"
    # LLM budget / cooldown
    BUDGET_HOURLY = "BUDGET_HOURLY"
    BUDGET_DAILY = "BUDGET_DAILY"
    BUDGET_TOKEN = "BUDGET_TOKEN"
    BUDGET_COST = "BUDGET_COST"
    BUDGET_REFLECTION = "BUDGET_REFLECTION"
    COOLDOWN_REPEATED_HOLD = "COOLDOWN_REPEATED_HOLD"
    COOLDOWN_REPEATED_INVALID = "COOLDOWN_REPEATED_INVALID"
    # LLM provider
    PROVIDER_CALL_STARTED = "PROVIDER_CALL_STARTED"
    PROVIDER_CALL_FAILED = "PROVIDER_CALL_FAILED"
    PROVIDER_RESPONSE_MALFORMED = "PROVIDER_RESPONSE_MALFORMED"
    PROVIDER_CALL_SUCCESS = "PROVIDER_CALL_SUCCESS"
    # Decision
    DECISION_BUY = "DECISION_BUY"
    DECISION_HOLD = "DECISION_HOLD"
    DECISION_SKIP_LOW_CONF = "DECISION_SKIP_LOW_CONF"
    DECISION_SKIP_LOW_EV = "DECISION_SKIP_LOW_EV"
    DECISION_SKIP_HIGH_SPREAD = "DECISION_SKIP_HIGH_SPREAD"
    DECISION_SKIP_EXPOSURE = "DECISION_SKIP_EXPOSURE"
    DECISION_SKIP_TTR = "DECISION_SKIP_TTR"
    # Execution
    EXEC_DISPATCHED = "EXEC_DISPATCHED"
    EXEC_DRY_RUN_SKIP = "EXEC_DRY_RUN_SKIP"
    EXEC_FAILED = "EXEC_FAILED"
    # Circuit breaker
    CB_OPEN = "CB_OPEN"
    CB_CLOSED = "CB_CLOSED"
    CB_OVERRIDE = "CB_OVERRIDE"
    # Alert
    ALERT_DISPATCHED = "ALERT_DISPATCHED"
    ALERT_DISPATCH_FAILED = "ALERT_DISPATCH_FAILED"
    # Recovery / error
    ERROR_HANDLED = "ERROR_HANDLED"
    ERROR_UNHANDLED = "ERROR_UNHANDLED"
    # Queue / persistence
    QUEUE_FULL = "QUEUE_FULL"
    QUEUE_DROPPED = "QUEUE_DROPPED"
    PERSIST_FAILED = "PERSIST_FAILED"
    PERSIST_SUCCESS = "PERSIST_SUCCESS"


class OperationalEventPersistenceStatus(str, Enum):
    """Persistence lifecycle status for operational event records."""

    PENDING = "PENDING"
    PERSISTED = "PERSISTED"
    FAILED = "FAILED"
    DROPPED = "DROPPED"


# ── Event Payload ──────────────────────────────────────────────────────────

# Extended secret-detection patterns for event payloads
_EVENT_FORBIDDEN_PATTERNS: list[tuple[str, re.Pattern]] = [
    *_FORBIDDEN_PAYLOAD_PATTERNS,
    ("wallet_address", re.compile(r"0x[a-fA-F0-9]{40}\b")),
    ("api_key_sk", re.compile(r"sk-[a-zA-Z0-9_-]{20,}\b")),
    ("api_key_pk", re.compile(r"pk-[a-zA-Z0-9_-]{20,}\b")),
    ("api_key_api", re.compile(r"api-[a-zA-Z0-9_-]{20,}\b")),
]

_EVENT_FORBIDDEN_SUBSTRINGS: list[str] = [
    *_FORBIDDEN_SUBSTRINGS,
    "raw_prompt",
    "reasoning_log",
    "reasoning_text",
    "raw reasoning",
    "raw prompt",
]


def _scan_event_payload(text: str) -> list[str]:
    """Scan event payload text for forbidden secret/cardinality patterns."""
    violations: list[str] = []
    for label, pattern in _EVENT_FORBIDDEN_PATTERNS:
        if pattern.search(text):
            violations.append(f"forbidden_pattern:{label}")
    for substr in _EVENT_FORBIDDEN_SUBSTRINGS:
        if substr.lower() in text.lower():
            violations.append(f"forbidden_substring:{substr}")
    return violations


class OperationalEventPayload(BaseModel):
    """Bounded, structured, secret-safe event payload.

    All fields are optional. When provided, they are validated against
    forbidden secret-like and high-cardinality content.
    """

    model_config = {"frozen": True}

    message: Optional[str] = Field(
        default=None,
        max_length=1024,
        description="Human-readable event description; must be secret-free",
    )
    reason_code: Optional[str] = Field(
        default=None,
        max_length=128,
        description="Stable reason code for this event instance",
    )
    dry_run: Optional[bool] = Field(
        default=None,
        description="Whether the agent was in dry-run mode at event time",
    )
    decision_action: Optional[str] = Field(
        default=None,
        max_length=16,
        description="Aggregate decision action (BUY/HOLD/SKIP)",
    )
    provider_name: Optional[str] = Field(
        default=None,
        max_length=64,
        description="Active LLM provider name at event time",
    )
    budget_remaining: Optional[Decimal] = Field(
        default=None,
        ge=0,
        description="Remaining LLM budget (calls or USD) at event time",
    )
    market_count: Optional[int] = Field(
        default=None,
        ge=0,
        description="Number of active or discovered markets",
    )
    ready_state: Optional[str] = Field(
        default=None,
        max_length=16,
        description="Current readiness state at event time",
    )

    @field_validator("message")
    @classmethod
    def _reject_forbidden_in_message(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        violations = _scan_event_payload(value)
        if violations:
            raise ValueError(f"message contains forbidden content: {violations}")
        return value

    @field_validator("reason_code")
    @classmethod
    def _reject_forbidden_in_reason_code(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        violations = _scan_event_payload(value)
        if violations:
            raise ValueError(f"reason_code contains forbidden content: {violations}")
        return value

    @field_validator("budget_remaining", mode="before")
    @classmethod
    def _reject_float_in_decimal_field(cls, value: Any) -> Optional[Decimal]:
        if value is None:
            return None
        if isinstance(value, float):
            raise ValueError("Float financial values are forbidden; use Decimal")
        if isinstance(value, Decimal):
            return value
        return Decimal(str(value))

    @field_validator("provider_name", "ready_state", "decision_action")
    @classmethod
    def _reject_large_strings_in_small_fields(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        violations = _scan_event_payload(value)
        if violations:
            raise ValueError(f"field contains forbidden content: {violations}")
        return value


# ── Creation & Record ─────────────────────────────────────────────────────


class OperationalEventCreate(BaseModel):
    """Request to create a new operational event for the ledger.

    This is what runtime code submits to the event bus.
    """

    model_config = {"frozen": True}

    event_type: OperationalEventType
    severity: OperationalEventSeverity
    source: OperationalEventSource
    reason_code: OperationalEventReasonCode
    payload: OperationalEventPayload = Field(default_factory=OperationalEventPayload)
    timestamp_utc: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="UTC timestamp when the event was created",
    )


class OperationalEventRecord(BaseModel):
    """A persisted operational event record as returned from the ledger."""

    model_config = {"frozen": True}

    id: str = Field(..., min_length=1, max_length=64, description="Unique event identifier")
    event_type: OperationalEventType
    severity: OperationalEventSeverity
    source: OperationalEventSource
    reason_code: OperationalEventReasonCode
    payload_json: str = Field(default="{}", description="JSON-serialized event payload")
    persistence_status: OperationalEventPersistenceStatus = Field(
        default=OperationalEventPersistenceStatus.PENDING,
    )
    created_at_utc: datetime = Field(..., description="UTC timestamp when persisted")
    recorded_at_utc: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="UTC timestamp when the record was created",
    )


# ── Batch ─────────────────────────────────────────────────────────────────


class OperationalEventBatch(BaseModel):
    """A batch of operational events to persist together."""

    model_config = {"frozen": True}

    events: list[OperationalEventCreate] = Field(
        ..., min_length=0, max_length=500, description="Events in this batch"
    )


class OperationalEventBatchResult(BaseModel):
    """Result of persisting a batch of operational events."""

    model_config = {"frozen": True}

    batch_id: str = Field(..., description="Identifier for this batch operation")
    total: int = Field(default=0, ge=0, description="Number of events in the batch")
    succeeded: int = Field(default=0, ge=0, description="Number successfully persisted")
    failed: int = Field(default=0, ge=0, description="Number that failed to persist")
    dropped: int = Field(default=0, ge=0, description="Number dropped due to overflow")
    records: list[OperationalEventRecord] = Field(
        default_factory=list, description="Persisted event records"
    )


# ── Append / Flush Results ────────────────────────────────────────────────


class OperationalEventAppendResult(BaseModel):
    """Result of appending a single event to the event bus queue."""

    model_config = {"frozen": True}

    accepted: bool = Field(..., description="True if the event was accepted into the queue")
    reason: Optional[str] = Field(
        default=None,
        max_length=256,
        description="Reason for rejection (e.g. queue_full) if not accepted",
    )
    queue_depth: int = Field(default=0, ge=0, description="Current queue depth after append")


class OperationalEventFlushResult(BaseModel):
    """Result of a batch flush from the event bus to the persistence layer."""

    model_config = {"frozen": True}

    batch_id: str = Field(..., description="Identifier for this flush operation")
    persisted: int = Field(default=0, ge=0, description="Number of events persisted")
    dropped: int = Field(default=0, ge=0, description="Number of events dropped")
    failed: int = Field(default=0, ge=0, description="Number of persistence failures")
    flush_duration_ms: Optional[float] = Field(
        default=None, description="Duration of this flush in milliseconds"
    )
    shutdown_flush: bool = Field(
        default=False, description="True if this was a final shutdown flush"
    )


# ── Queue State & Policy ──────────────────────────────────────────────────


class OperationalEventQueueState(BaseModel):
    """Snapshot of the operational event bus queue state."""

    model_config = {"frozen": True}

    current_depth: int = Field(default=0, ge=0, description="Number of events currently queued")
    max_capacity: int = Field(default=0, ge=0, description="Maximum queue capacity")
    dropped_total: int = Field(default=0, ge=0, description="Total events dropped since start")
    overflow: bool = Field(default=False, description="True if queue has overflowed at least once")
    last_overflow_at_utc: Optional[datetime] = Field(
        default=None, description="UTC timestamp of last overflow event"
    )


class OperationalEventQueuePolicy(BaseModel):
    """Queue overflow and priority policy for the event bus."""

    model_config = {"frozen": True}

    max_size: int = Field(default=1000, ge=10, le=100000, description="Maximum queue capacity")
    overflow_behavior: Literal["drop_oldest", "drop_newest", "drop_diagnostic"] = Field(
        default="drop_oldest",
        description="Overflow strategy: drop_oldest, drop_newest, or drop_diagnostic",
    )
    critical_severities: list[OperationalEventSeverity] = Field(
        default_factory=lambda: [OperationalEventSeverity.CRITICAL, OperationalEventSeverity.ERROR],
        description="Severities that are never dropped during overflow",
    )


# ── Query / Read Window ───────────────────────────────────────────────────


class OperationalEventQuery(BaseModel):
    """Filter parameters for querying operational events from the ledger."""

    model_config = {"frozen": True}

    event_types: Optional[list[OperationalEventType]] = Field(
        default=None, description="Filter by event type(s)"
    )
    severities: Optional[list[OperationalEventSeverity]] = Field(
        default=None, description="Filter by severity level(s)"
    )
    sources: Optional[list[OperationalEventSource]] = Field(
        default=None, description="Filter by source component(s)"
    )
    reason_codes: Optional[list[OperationalEventReasonCode]] = Field(
        default=None, description="Filter by reason code(s)"
    )
    start_time_utc: Optional[datetime] = Field(
        default=None, description="Include events at or after this UTC time"
    )
    end_time_utc: Optional[datetime] = Field(
        default=None, description="Include events at or before this UTC time"
    )
    limit: int = Field(default=100, ge=1, le=1000, description="Maximum records to return")
    offset: int = Field(
        default=0,
        ge=0,
        description="Number of matching records to skip before returning results (pagination cursor)",
    )


class OperationalEventReadWindow(BaseModel):
    """Time-bounded result window from reading the operational event ledger."""

    model_config = {"frozen": True}

    events: list[OperationalEventRecord] = Field(default_factory=list)
    start_time_utc: Optional[datetime] = Field(default=None)
    end_time_utc: Optional[datetime] = Field(default=None)
    total_count: int = Field(default=0, ge=0, description="Total matching records in the window")
    has_more: bool = Field(
        default=False, description="True if more records exist beyond this window"
    )


# ── Validation / Redaction ────────────────────────────────────────────────


class OperationalEventValidationError(BaseModel):
    """Validation error for an operational event creation attempt."""

    model_config = {"frozen": True}

    event_id: Optional[str] = Field(
        default=None, description="ID of the event that failed validation, if known"
    )
    violations: list[str] = Field(
        default_factory=list, description="List of validation violation descriptions"
    )
    field_errors: dict[str, str] = Field(
        default_factory=dict, description="Field-specific error messages"
    )


class OperationalEventRedactionResult(BaseModel):
    """Result of redacting secret/high-cardinality content from an event payload."""

    model_config = {"frozen": True}

    redaction_occurred: bool = Field(
        default=False, description="True if any content was redacted"
    )
    redacted_fields: list[str] = Field(
        default_factory=list, description="Names of fields that had content redacted"
    )
    original_event_type: Optional[OperationalEventType] = Field(
        default=None, description="Event type of the original event"
    )


# ═══════════════════════════════════════════════════════════════════════════
# WI-57 — Deterministic Human Narratives
#
# Presentation-layer schemas that convert typed operational ledger events
# into plain-English operator summaries. These schemas are intentionally
# separate from LLMEvaluationResponse (Gatekeeper) and from cognitive /
# execution schemas; the narrative layer is read-only and never persists.
# ═══════════════════════════════════════════════════════════════════════════


class NarrativeRenderStatus(str, Enum):
    """Outcome status for a single narrative render attempt."""

    SUCCESS = "SUCCESS"
    FALLBACK = "FALLBACK"
    REDACTED = "REDACTED"
    FAILED = "FAILED"


class NarrativeRenderFailureReason(str, Enum):
    """Stable reasons a narrative render fell back or failed."""

    MALFORMED_PAYLOAD_JSON = "MALFORMED_PAYLOAD_JSON"
    FORBIDDEN_CONTENT = "FORBIDDEN_CONTENT"
    UNKNOWN_TEMPLATE = "UNKNOWN_TEMPLATE"
    NAIVE_TIMESTAMP = "NAIVE_TIMESTAMP"
    INVALID_INPUT = "INVALID_INPUT"


class NarrativeTemplateKey(str, Enum):
    """Stable, bounded template keys for deterministic narrative rendering.

    Each key maps a supported (event_type, reason_code) family to a fixed
    English template. Adding a new mapping requires extending this enum.
    """

    # Lifecycle
    LIFECYCLE_START = "LIFECYCLE_START"
    LIFECYCLE_SHUTDOWN = "LIFECYCLE_SHUTDOWN"
    CONFIG_LOADED = "CONFIG_LOADED"
    # Markets
    MARKET_DISCOVERED = "MARKET_DISCOVERED"
    MARKET_REJECTED_INELIGIBLE = "MARKET_REJECTED_INELIGIBLE"
    MARKET_REJECTED_NOT_FOUND = "MARKET_REJECTED_NOT_FOUND"
    MARKET_REJECTED_COOLDOWN = "MARKET_REJECTED_COOLDOWN"
    MARKET_QUARANTINED = "MARKET_QUARANTINED"
    # WebSocket
    WS_CONNECTED = "WS_CONNECTED"
    WS_RECONNECT = "WS_RECONNECT"
    WS_PONG_STALE = "WS_PONG_STALE"
    # Readiness
    READINESS_READY = "READINESS_READY"
    READINESS_DEGRADED = "READINESS_DEGRADED"
    READINESS_NOT_READY = "READINESS_NOT_READY"
    # LLM budget / cooldown
    BUDGET_DAILY = "BUDGET_DAILY"
    BUDGET_HOURLY = "BUDGET_HOURLY"
    BUDGET_TOKEN = "BUDGET_TOKEN"
    BUDGET_COST = "BUDGET_COST"
    BUDGET_REFLECTION = "BUDGET_REFLECTION"
    COOLDOWN_REPEATED_HOLD = "COOLDOWN_REPEATED_HOLD"
    COOLDOWN_REPEATED_INVALID = "COOLDOWN_REPEATED_INVALID"
    # Provider
    PROVIDER_CALL_FAILED = "PROVIDER_CALL_FAILED"
    PROVIDER_RESPONSE_MALFORMED = "PROVIDER_RESPONSE_MALFORMED"
    # Decisions
    DECISION_ACCEPTED_BUY = "DECISION_ACCEPTED_BUY"
    DECISION_ACCEPTED_HOLD = "DECISION_ACCEPTED_HOLD"
    DECISION_SKIP_LOW_CONF = "DECISION_SKIP_LOW_CONF"
    DECISION_SKIP_LOW_EV = "DECISION_SKIP_LOW_EV"
    DECISION_SKIP_HIGH_SPREAD = "DECISION_SKIP_HIGH_SPREAD"
    DECISION_SKIP_EXPOSURE = "DECISION_SKIP_EXPOSURE"
    DECISION_SKIP_TTR = "DECISION_SKIP_TTR"
    # Execution
    EXECUTION_DRY_RUN = "EXECUTION_DRY_RUN"
    # Circuit breaker
    CIRCUIT_BREAKER_OPEN = "CIRCUIT_BREAKER_OPEN"
    CIRCUIT_BREAKER_CLOSED = "CIRCUIT_BREAKER_CLOSED"
    CIRCUIT_BREAKER_OVERRIDE = "CIRCUIT_BREAKER_OVERRIDE"
    # Alerts
    ALERT_DISPATCHED = "ALERT_DISPATCHED"
    ALERT_DISPATCH_FAILED = "ALERT_DISPATCH_FAILED"
    # Recovery / error
    ERROR_HANDLED = "ERROR_HANDLED"
    ERROR_UNHANDLED = "ERROR_UNHANDLED"
    # Generic / catch-all
    GENERIC = "GENERIC"


class NarrativeInspectionHint(BaseModel):
    """Bounded, secret-safe pointer to what an operator should inspect next.

    Component and pointer values are drawn from low-cardinality enums or
    short stable strings. Never carries token IDs, condition IDs, wallet
    addresses, or raw payload data.
    """

    model_config = {"frozen": True}

    component: OperationalEventSource = Field(
        ..., description="Source component the operator should inspect"
    )
    pointer: str = Field(
        default="",
        max_length=64,
        description="Short stable pointer (e.g. 'readiness', 'budget', 'breaker')",
    )
    severity: OperationalEventSeverity = Field(
        default=OperationalEventSeverity.INFO,
        description="Severity of the situation prompting inspection",
    )

    @field_validator("pointer")
    @classmethod
    def _reject_forbidden_in_pointer(cls, value: str) -> str:
        if value == "":
            return value
        violations = _scan_event_payload(value)
        if violations:
            raise ValueError(f"pointer contains forbidden content: {violations}")
        return value


class OperationalNarrative(BaseModel):
    """Plain-English operator summary of a single operational event."""

    model_config = {"frozen": True}

    event_type: OperationalEventType
    severity: OperationalEventSeverity
    source: OperationalEventSource
    reason_code: OperationalEventReasonCode
    template_key: NarrativeTemplateKey
    summary: str = Field(
        ..., min_length=1, max_length=1024,
        description="Deterministic plain-English operator summary",
    )
    continuation_state: Optional[str] = Field(
        default=None,
        max_length=32,
        description="continued / skipped / degraded / stopped — when inferable",
    )
    inspection_hint: Optional[NarrativeInspectionHint] = None
    timestamp_utc: Optional[datetime] = Field(
        default=None,
        description="Event timestamp normalized to UTC, when available",
    )
    dry_run: Optional[bool] = Field(
        default=None,
        description="dry-run status at event time (read-only display field)",
    )

    @field_validator("summary")
    @classmethod
    def _reject_forbidden_in_summary(cls, value: str) -> str:
        violations = _scan_event_payload(value)
        if violations:
            raise ValueError(f"summary contains forbidden content: {violations}")
        return value

    @field_validator("continuation_state")
    @classmethod
    def _validate_continuation_state(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        if value not in {"continued", "skipped", "degraded", "stopped"}:
            raise ValueError(
                "continuation_state must be one of: continued, skipped, degraded, stopped"
            )
        return value


class DecisionNarrative(BaseModel):
    """Plain-English operator summary of an evaluation decision outcome."""

    model_config = {"frozen": True}

    event_type: OperationalEventType
    severity: OperationalEventSeverity
    source: OperationalEventSource
    reason_code: OperationalEventReasonCode
    template_key: NarrativeTemplateKey
    decision_action: Optional[str] = Field(
        default=None,
        max_length=16,
        description="Aggregate action (BUY/HOLD/SKIP)",
    )
    summary: str = Field(..., min_length=1, max_length=1024)
    continuation_state: Optional[str] = Field(default=None, max_length=32)
    inspection_hint: Optional[NarrativeInspectionHint] = None
    timestamp_utc: Optional[datetime] = None
    dry_run: Optional[bool] = None

    @field_validator("summary")
    @classmethod
    def _reject_forbidden_in_summary(cls, value: str) -> str:
        violations = _scan_event_payload(value)
        if violations:
            raise ValueError(f"summary contains forbidden content: {violations}")
        return value

    @field_validator("decision_action")
    @classmethod
    def _validate_decision_action(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        violations = _scan_event_payload(value)
        if violations:
            raise ValueError(f"decision_action contains forbidden content: {violations}")
        return value

    @field_validator("continuation_state")
    @classmethod
    def _validate_continuation_state(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        if value not in {"continued", "skipped", "degraded", "stopped"}:
            raise ValueError(
                "continuation_state must be one of: continued, skipped, degraded, stopped"
            )
        return value


class RuntimeNarrative(BaseModel):
    """Discriminated wrapper around an operational or decision narrative.

    Future surfaces (incident replay, dashboard timeline, daily digest)
    consume RuntimeNarrative without caring whether the underlying record
    is an operational lifecycle event or a decision event.
    """

    model_config = {"frozen": True}

    kind: Literal["operational", "decision"]
    operational: Optional[OperationalNarrative] = None
    decision: Optional[DecisionNarrative] = None

    @model_validator(mode="after")
    def _validate_exactly_one(self) -> "RuntimeNarrative":
        if self.kind == "operational" and self.operational is None:
            raise ValueError("kind=operational requires operational narrative")
        if self.kind == "decision" and self.decision is None:
            raise ValueError("kind=decision requires decision narrative")
        if self.kind == "operational" and self.decision is not None:
            raise ValueError("kind=operational must not carry decision narrative")
        if self.kind == "decision" and self.operational is not None:
            raise ValueError("kind=decision must not carry operational narrative")
        return self


class NarrativeRenderResult(BaseModel):
    """Typed result of a single narrative render attempt.

    Render never raises for supported inputs; it returns a typed status
    plus an optional narrative payload and optional failure reason.
    """

    model_config = {"frozen": True}

    status: NarrativeRenderStatus
    narrative: Optional[RuntimeNarrative] = None
    failure_reason: Optional[NarrativeRenderFailureReason] = None
    detail: Optional[str] = Field(
        default=None,
        max_length=256,
        description="Short, secret-safe explanation for non-SUCCESS results",
    )

    @field_validator("detail")
    @classmethod
    def _reject_forbidden_in_detail(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        violations = _scan_event_payload(value)
        if violations:
            raise ValueError(f"detail contains forbidden content: {violations}")
        return value

    @model_validator(mode="after")
    def _validate_status_consistency(self) -> "NarrativeRenderResult":
        if self.status == NarrativeRenderStatus.SUCCESS and self.narrative is None:
            raise ValueError("SUCCESS status requires a narrative")
        if self.status == NarrativeRenderStatus.FAILED and self.failure_reason is None:
            raise ValueError("FAILED status requires a failure_reason")
        return self


# ═══════════════════════════════════════════════════════════════════════════
# WI-58 — Incident Replay CLI
#
# Read-only presentation schemas for the operator-facing incident replay
# surface. These types compose the WI-56 operational event ledger and the
# WI-57 deterministic narrative layer into a bounded, secret-safe replay
# report. They never persist, never mutate, and never bypass the
# Gatekeeper.
# ═══════════════════════════════════════════════════════════════════════════


class IncidentReplayStatus(str, Enum):
    """Outcome status of a single incident replay invocation."""

    SUCCESS = "SUCCESS"
    EMPTY_WINDOW = "EMPTY_WINDOW"
    INVALID_WINDOW = "INVALID_WINDOW"
    INVALID_TIMESTAMP = "INVALID_TIMESTAMP"
    INVALID_FILTER = "INVALID_FILTER"
    REPOSITORY_FAILURE = "REPOSITORY_FAILURE"
    DATABASE_UNAVAILABLE = "DATABASE_UNAVAILABLE"
    TRUNCATED = "TRUNCATED"


class IncidentReplayFailureReason(str, Enum):
    """Typed failure reasons for non-success replay outcomes."""

    FROM_AFTER_TO = "FROM_AFTER_TO"
    MALFORMED_TIMESTAMP = "MALFORMED_TIMESTAMP"
    NAIVE_TIMESTAMP = "NAIVE_TIMESTAMP"
    UNKNOWN_ENUM_VALUE = "UNKNOWN_ENUM_VALUE"
    LIMIT_OUT_OF_RANGE = "LIMIT_OUT_OF_RANGE"
    REPOSITORY_ERROR = "REPOSITORY_ERROR"
    MISSING_EVENT_TABLE = "MISSING_EVENT_TABLE"
    DATABASE_UNREACHABLE = "DATABASE_UNREACHABLE"
    FORBIDDEN_CONTENT = "FORBIDDEN_CONTENT"
    RESULT_TRUNCATED = "RESULT_TRUNCATED"


class IncidentReplayFilter(BaseModel):
    """Typed filter for incident replay.

    Every filter field is an optional list of typed enum values. Free-form
    strings are rejected at the schema boundary. Filters are independent
    and combinable; combined filters intersect.
    """

    model_config = {"frozen": True}

    severities: Optional[list[OperationalEventSeverity]] = Field(
        default=None,
        description="Filter to one or more typed severities (independent).",
    )
    sources: Optional[list[OperationalEventSource]] = Field(
        default=None,
        description="Filter to one or more typed source components (independent).",
    )
    event_types: Optional[list[OperationalEventType]] = Field(
        default=None,
        description="Filter to one or more typed event types (independent).",
    )
    reason_codes: Optional[list[OperationalEventReasonCode]] = Field(
        default=None,
        description="Filter to one or more typed reason codes (independent).",
    )

    def is_empty(self) -> bool:
        """True when no filter narrows the result set."""
        return (
            not self.severities
            and not self.sources
            and not self.event_types
            and not self.reason_codes
        )


class IncidentReplayRequest(BaseModel):
    """Bounded UTC time window request for incident replay.

    ``from_utc`` and ``to_utc`` must be timezone-aware and normalized to
    UTC. ``from_utc`` must be earlier than or equal to ``to_utc``.
    """

    model_config = {"frozen": True}

    from_utc: datetime = Field(..., description="Window start (timezone-aware UTC).")
    to_utc: datetime = Field(..., description="Window end (timezone-aware UTC).")
    filter: IncidentReplayFilter = Field(
        default_factory=IncidentReplayFilter,
        description="Typed replay filter.",
    )
    limit: int = Field(
        default=1000, ge=1, le=1000,
        description="Maximum replay lines to return; matches repository window cap.",
    )

    @field_validator("from_utc", "to_utc")
    @classmethod
    def _require_tzaware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _validate_window(self) -> "IncidentReplayRequest":
        if self.from_utc > self.to_utc:
            raise ValueError("from_utc must be earlier than or equal to to_utc")
        return self


class IncidentReplayLine(BaseModel):
    """One typed, secret-safe line of replay output.

    The line never carries the raw payload, raw reasoning, raw provider
    response, token IDs, condition IDs, wallet addresses, or unbounded
    text. The ``summary`` is the deterministic WI-57 narrative summary
    after secret-scan validation.
    """

    model_config = {"frozen": True}

    event_id: str = Field(..., min_length=1, max_length=64)
    event_type: OperationalEventType
    severity: OperationalEventSeverity
    source: OperationalEventSource
    reason_code: OperationalEventReasonCode
    template_key: NarrativeTemplateKey
    narrative_status: NarrativeRenderStatus
    summary: str = Field(..., min_length=1, max_length=1024)
    continuation_state: Optional[str] = Field(default=None, max_length=32)
    dry_run: Optional[bool] = Field(default=None)
    timestamp_utc: datetime = Field(..., description="UTC event creation time.")

    @field_validator("summary")
    @classmethod
    def _scan_summary_for_forbidden(cls, value: str) -> str:
        violations = _scan_event_payload(value)
        if violations:
            raise ValueError(f"summary contains forbidden content: {violations}")
        return value

    @field_validator("timestamp_utc")
    @classmethod
    def _require_tzaware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamp_utc must be timezone-aware")
        return value.astimezone(timezone.utc)


class IncidentReplaySummary(BaseModel):
    """Bounded, typed aggregate counts for a replay window.

    Counts are derived from typed event fields only. ``markets_seen`` is
    a bounded count of typed market-discovery / market-rejection events
    and contains no token IDs, condition IDs, or raw market names.
    """

    model_config = {"frozen": True}

    total_events: int = Field(default=0, ge=0)
    warnings: int = Field(default=0, ge=0)
    errors: int = Field(default=0, ge=0)
    markets_seen: int = Field(default=0, ge=0)
    decisions_by_action: dict[str, int] = Field(default_factory=dict)
    skips_by_reason: dict[str, int] = Field(default_factory=dict)
    llm_calls: int = Field(default=0, ge=0)
    budget_blocks: int = Field(default=0, ge=0)
    cooldown_blocks: int = Field(default=0, ge=0)
    provider_failures: int = Field(default=0, ge=0)
    readiness_changes: int = Field(default=0, ge=0)

    @field_validator("decisions_by_action")
    @classmethod
    def _validate_decisions_keys(cls, value: dict[str, int]) -> dict[str, int]:
        # Only typed aggregate actions are allowed; reject any free-form text.
        allowed = {"BUY", "HOLD", "SKIP"}
        for key in value:
            if key not in allowed:
                raise ValueError(
                    f"decisions_by_action key must be typed action (BUY/HOLD/SKIP); got {key!r}"
                )
        return value

    @field_validator("skips_by_reason")
    @classmethod
    def _validate_skip_keys(cls, value: dict[str, int]) -> dict[str, int]:
        # Skip-reason keys must be stable typed reason-code values.
        allowed = {code.value for code in OperationalEventReasonCode}
        for key in value:
            if key not in allowed:
                raise ValueError(
                    f"skips_by_reason key must be typed OperationalEventReasonCode value; got {key!r}"
                )
        return value


class IncidentReplayReport(BaseModel):
    """Top-level typed report for a single incident replay invocation."""

    model_config = {"frozen": True}

    status: IncidentReplayStatus
    request: IncidentReplayRequest
    lines: list[IncidentReplayLine] = Field(default_factory=list)
    summary: IncidentReplaySummary = Field(default_factory=IncidentReplaySummary)
    failure_reason: Optional[IncidentReplayFailureReason] = None
    message: Optional[str] = Field(
        default=None,
        max_length=256,
        description="Short, secret-safe explanation for non-success outcomes.",
    )
    has_more: bool = Field(
        default=False,
        description="True when the repository indicated more matching rows were available.",
    )

    @field_validator("message")
    @classmethod
    def _scan_message_for_forbidden(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        violations = _scan_event_payload(value)
        if violations:
            raise ValueError(f"message contains forbidden content: {violations}")
        return value

    @model_validator(mode="after")
    def _validate_status_consistency(self) -> "IncidentReplayReport":
        non_success = {
            IncidentReplayStatus.INVALID_WINDOW,
            IncidentReplayStatus.INVALID_TIMESTAMP,
            IncidentReplayStatus.INVALID_FILTER,
            IncidentReplayStatus.REPOSITORY_FAILURE,
            IncidentReplayStatus.DATABASE_UNAVAILABLE,
        }
        if self.status in non_success and self.failure_reason is None:
            raise ValueError(f"status {self.status.value} requires a failure_reason")
        if self.status == IncidentReplayStatus.SUCCESS and not self.lines:
            raise ValueError(
                "status SUCCESS requires at least one replay line; use EMPTY_WINDOW for zero-event reports"
            )
        return self


# ═══════════════════════════════════════════════════════════════════════════
# WI-59 — Dashboard Activity Feed
#
# Read-only presentation schemas for the operator-facing Streamlit
# dashboard activity timeline and "what is the bot doing right now?"
# current-state panel. These types compose the WI-56 operational event
# ledger and the WI-57 deterministic narrative layer into a bounded,
# secret-safe feed for the dashboard. They never persist, never mutate,
# and never bypass the Gatekeeper.
# ═══════════════════════════════════════════════════════════════════════════


class DashboardActivityFeedStatus(str, Enum):
    """Outcome status of a dashboard activity feed fetch."""

    SUCCESS = "SUCCESS"
    EMPTY_WINDOW = "EMPTY_WINDOW"
    DATABASE_UNAVAILABLE = "DATABASE_UNAVAILABLE"
    MISSING_TABLE = "MISSING_TABLE"
    TRUNCATED = "TRUNCATED"


class DashboardActivityFeedFailureReason(str, Enum):
    """Typed failure reasons for non-success feed outcomes."""

    MISSING_TABLE = "MISSING_TABLE"
    DATABASE_UNREACHABLE = "DATABASE_UNREACHABLE"
    RESULT_TRUNCATED = "RESULT_TRUNCATED"
    FORBIDDEN_CONTENT = "FORBIDDEN_CONTENT"


class DashboardActivityFeedFilter(BaseModel):
    """Typed filter for the dashboard activity feed.

    Every filter field is an optional list of typed enum values. Free-form
    strings are rejected at the schema boundary. Filters are independent
    and combinable; combined filters intersect.
    """

    model_config = {"frozen": True}

    severities: Optional[list[OperationalEventSeverity]] = Field(
        default=None,
        description="Filter to one or more typed severities (independent).",
    )
    sources: Optional[list[OperationalEventSource]] = Field(
        default=None,
        description="Filter to one or more typed source components (independent).",
    )
    event_types: Optional[list[OperationalEventType]] = Field(
        default=None,
        description="Filter to one or more typed event types (independent).",
    )
    reason_codes: Optional[list[OperationalEventReasonCode]] = Field(
        default=None,
        description="Filter to one or more typed reason codes (independent).",
    )

    def is_empty(self) -> bool:
        """True when no filter narrows the result set."""
        return (
            not self.severities
            and not self.sources
            and not self.event_types
            and not self.reason_codes
        )


class DashboardActivityFeedItem(BaseModel):
    """One typed, secret-safe row of the dashboard activity timeline.

    The row never carries the raw payload, raw reasoning, raw provider
    response, token IDs, condition IDs, wallet addresses, or unbounded
    text. The ``summary`` is the deterministic WI-57 narrative summary
    after secret-scan validation.
    """

    model_config = {"frozen": True}

    event_id: str = Field(..., min_length=1, max_length=64)
    event_type: OperationalEventType
    severity: OperationalEventSeverity
    source: OperationalEventSource
    reason_code: OperationalEventReasonCode
    template_key: NarrativeTemplateKey
    narrative_status: NarrativeRenderStatus
    summary: str = Field(..., min_length=1, max_length=1024)
    continuation_state: Optional[str] = Field(default=None, max_length=32)
    dry_run: Optional[bool] = Field(default=None)
    timestamp_utc: datetime = Field(..., description="UTC event creation time.")

    @field_validator("summary")
    @classmethod
    def _scan_summary_for_forbidden(cls, value: str) -> str:
        violations = _scan_event_payload(value)
        if violations:
            raise ValueError(f"summary contains forbidden content: {violations}")
        return value

    @field_validator("timestamp_utc")
    @classmethod
    def _require_tzaware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamp_utc must be timezone-aware")
        return value.astimezone(timezone.utc)

    @field_validator("continuation_state")
    @classmethod
    def _validate_continuation_state(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        if value not in {"continued", "skipped", "degraded", "stopped"}:
            raise ValueError(
                "continuation_state must be one of: continued, skipped, degraded, stopped"
            )
        return value


class DashboardCurrentState(BaseModel):
    """Bounded, typed "what is the bot doing right now?" panel state.

    Each field summarizes the latest persisted typed event for its
    category. Fields are ``None`` when no recent typed event supports
    that category. ``overall_state`` is one of the WI-57 continuation
    states or ``unknown``. All summaries are secret-safe.
    """

    model_config = {"frozen": True}

    lifecycle_summary: Optional[str] = Field(default=None, max_length=512)
    readiness_summary: Optional[str] = Field(default=None, max_length=512)
    websocket_summary: Optional[str] = Field(default=None, max_length=512)
    llm_summary: Optional[str] = Field(default=None, max_length=512)
    decision_summary: Optional[str] = Field(default=None, max_length=512)
    execution_summary: Optional[str] = Field(default=None, max_length=512)
    circuit_breaker_summary: Optional[str] = Field(default=None, max_length=512)
    overall_state: str = Field(
        default="unknown",
        max_length=16,
        description="continued / skipped / degraded / stopped / unknown",
    )
    timestamp_utc: datetime = Field(
        ...,
        description="UTC timestamp when this state snapshot was derived.",
    )

    @field_validator(
        "lifecycle_summary",
        "readiness_summary",
        "websocket_summary",
        "llm_summary",
        "decision_summary",
        "execution_summary",
        "circuit_breaker_summary",
    )
    @classmethod
    def _scan_summary_for_forbidden(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        violations = _scan_event_payload(value)
        if violations:
            raise ValueError(f"summary contains forbidden content: {violations}")
        return value

    @field_validator("overall_state")
    @classmethod
    def _validate_overall_state(cls, value: str) -> str:
        allowed = {"continued", "skipped", "degraded", "stopped", "unknown"}
        if value not in allowed:
            raise ValueError(
                f"overall_state must be one of {sorted(allowed)}; got {value!r}"
            )
        return value

    @field_validator("timestamp_utc")
    @classmethod
    def _require_tzaware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamp_utc must be timezone-aware")
        return value.astimezone(timezone.utc)


class DashboardActivityFeedResult(BaseModel):
    """Typed result of fetching the dashboard activity feed.

    SUCCESS requires at least one item AND a non-None current_state.
    EMPTY_WINDOW renders an empty timeline and may have ``current_state=None``.
    Non-success non-empty statuses (DATABASE_UNAVAILABLE, MISSING_TABLE,
    TRUNCATED) require a typed ``failure_reason``.
    """

    model_config = {"frozen": True}

    status: DashboardActivityFeedStatus
    items: list[DashboardActivityFeedItem] = Field(default_factory=list)
    current_state: Optional[DashboardCurrentState] = None
    failure_reason: Optional[DashboardActivityFeedFailureReason] = None
    message: Optional[str] = Field(
        default=None,
        max_length=256,
        description="Short, secret-safe explanation for non-success outcomes.",
    )
    has_more: bool = Field(
        default=False,
        description="True when more matching rows were available beyond the limit.",
    )

    @field_validator("message")
    @classmethod
    def _scan_message_for_forbidden(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        violations = _scan_event_payload(value)
        if violations:
            raise ValueError(f"message contains forbidden content: {violations}")
        return value

    @model_validator(mode="after")
    def _validate_status_consistency(self) -> "DashboardActivityFeedResult":
        non_success = {
            DashboardActivityFeedStatus.DATABASE_UNAVAILABLE,
            DashboardActivityFeedStatus.MISSING_TABLE,
            DashboardActivityFeedStatus.TRUNCATED,
        }
        if self.status in non_success and self.failure_reason is None:
            raise ValueError(
                f"status {self.status.value} requires a failure_reason"
            )
        if self.status == DashboardActivityFeedStatus.SUCCESS and not self.items:
            raise ValueError(
                "status SUCCESS requires at least one feed item; "
                "use EMPTY_WINDOW for zero-event feeds"
            )
        if (
            self.status == DashboardActivityFeedStatus.SUCCESS
            and self.current_state is None
        ):
            raise ValueError(
                "status SUCCESS requires a current_state derived from recent events"
            )
        return self


# ═══════════════════════════════════════════════════════════════════════════
# WI-60 — Daily Operations Digest
#
# Typed schemas for a deterministic daily operator digest produced over
# the WI-56 operational event ledger, WI-57 narrative layer, WI-58 replay
# summary patterns, and WI-59 dashboard current-state semantics. The
# digest writes to ``03_Daily/YYYY-MM-DD-bot.md`` and never overwrites
# manual notes at ``03_Daily/YYYY-MM-DD.md``. It never persists events,
# never modifies LLMEvaluationResponse, and never bypasses the
# Gatekeeper.
# ═══════════════════════════════════════════════════════════════════════════


class DailyOpsDigestStatus(str, Enum):
    """Outcome status of a single daily ops digest invocation."""

    SUCCESS = "SUCCESS"
    EMPTY_WINDOW = "EMPTY_WINDOW"
    DATABASE_UNAVAILABLE = "DATABASE_UNAVAILABLE"
    MISSING_TABLE = "MISSING_TABLE"
    REPOSITORY_FAILURE = "REPOSITORY_FAILURE"
    PATH_FAILURE = "PATH_FAILURE"
    FORBIDDEN_CONTENT = "FORBIDDEN_CONTENT"
    INVALID_REQUEST = "INVALID_REQUEST"
    # Fail-closed status for windows that exceed the bounded event-read
    # budget. Partial aggregates would silently undercount, so the digest
    # refuses to write rather than emit incomplete totals.
    READ_CAP_REACHED = "READ_CAP_REACHED"


class DailyOpsDigestFailureReason(str, Enum):
    """Typed failure reasons for non-success digest outcomes."""

    DATABASE_UNREACHABLE = "DATABASE_UNREACHABLE"
    MISSING_TABLE = "MISSING_TABLE"
    REPOSITORY_ERROR = "REPOSITORY_ERROR"
    PATH_OUTSIDE_DAILY = "PATH_OUTSIDE_DAILY"
    MANUAL_NOTE_WOULD_OVERWRITE = "MANUAL_NOTE_WOULD_OVERWRITE"
    INVALID_FILENAME = "INVALID_FILENAME"
    FORBIDDEN_CONTENT = "FORBIDDEN_CONTENT"
    INVALID_DATE = "INVALID_DATE"
    READ_CAP_REACHED = "READ_CAP_REACHED"


class DailyOpsDigestWindow(BaseModel):
    """Bounded UTC time window for digest computation.

    ``from_utc`` and ``to_utc`` must be timezone-aware. ``from_utc`` must
    be earlier than or equal to ``to_utc``.
    """

    model_config = {"frozen": True}

    from_utc: datetime = Field(..., description="Window start (timezone-aware UTC).")
    to_utc: datetime = Field(..., description="Window end (timezone-aware UTC).")

    @field_validator("from_utc", "to_utc")
    @classmethod
    def _require_tzaware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _validate_window(self) -> "DailyOpsDigestWindow":
        if self.from_utc > self.to_utc:
            raise ValueError("from_utc must be earlier than or equal to to_utc")
        return self


class DailyOpsDigestRequest(BaseModel):
    """Request envelope for daily digest generation.

    ``digest_date_utc`` identifies the calendar date for which to produce
    a digest. The service derives the daily window ``[00:00, 24:00)`` UTC
    from this date unless an explicit ``window`` is provided.
    """

    model_config = {"frozen": True}

    digest_date_utc: datetime = Field(
        ..., description="Digest date (timezone-aware UTC). Hour/minute ignored."
    )
    window: Optional[DailyOpsDigestWindow] = Field(
        default=None,
        description="Explicit UTC window; when None the daily window is derived.",
    )
    output_path: Optional[str] = Field(
        default=None,
        max_length=512,
        description=(
            "Optional output path override. Must end with "
            "'YYYY-MM-DD-bot.md' and reside under the configured daily notes "
            "directory; otherwise the service fails closed."
        ),
    )
    daily_notes_dir: str = Field(
        default="03_Daily",
        max_length=128,
        description="Relative or absolute path to the vault daily notes directory.",
    )
    enable_telegram: bool = Field(
        default=False,
        description=(
            "Operator-requested Telegram delivery. The service still requires "
            "Telegram alerts to be enabled at config level before sending."
        ),
    )

    @field_validator("digest_date_utc")
    @classmethod
    def _require_tzaware_date(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("digest_date_utc must be timezone-aware")
        return value.astimezone(timezone.utc)


class DailyOpsDigestRunSummary(BaseModel):
    """Typed run-lifecycle summary for the digest window.

    Run status is one of:

    * ``completed`` — matched START and SHUTDOWN events.
    * ``partial`` — START observed but no SHUTDOWN within window.
    * ``no_run`` — no lifecycle events observed.
    * ``unknown`` — typed evidence is insufficient.
    """

    model_config = {"frozen": True}

    start_utc: Optional[datetime] = None
    stop_utc: Optional[datetime] = None
    uptime_seconds: Optional[int] = Field(default=None, ge=0)
    run_status: str = Field(default="unknown", max_length=16)
    active_provider: Optional[str] = Field(default=None, max_length=64)
    dry_run: Optional[bool] = None
    latest_readiness: Optional[str] = Field(default=None, max_length=16)
    markets_seen: int = Field(default=0, ge=0)
    markets_rejected: int = Field(default=0, ge=0)

    @field_validator("start_utc", "stop_utc")
    @classmethod
    def _validate_tzaware(cls, value: Optional[datetime]) -> Optional[datetime]:
        if value is None:
            return value
        if value.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        return value.astimezone(timezone.utc)

    @field_validator("run_status")
    @classmethod
    def _validate_run_status(cls, value: str) -> str:
        allowed = {"completed", "partial", "no_run", "unknown"}
        if value not in allowed:
            raise ValueError(
                f"run_status must be one of {sorted(allowed)}; got {value!r}"
            )
        return value

    @field_validator("active_provider", "latest_readiness")
    @classmethod
    def _scan_low_card_fields(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        violations = _scan_event_payload(value)
        if violations:
            raise ValueError(f"field contains forbidden content: {violations}")
        return value


class DailyOpsDigestDecisionSummary(BaseModel):
    """Decisions and skips, counted from typed reason codes only."""

    model_config = {"frozen": True}

    accepted_buy: int = Field(default=0, ge=0)
    accepted_hold: int = Field(default=0, ge=0)
    skipped_low_conf: int = Field(default=0, ge=0)
    skipped_low_ev: int = Field(default=0, ge=0)
    skipped_high_spread: int = Field(default=0, ge=0)
    skipped_exposure: int = Field(default=0, ge=0)
    skipped_ttr: int = Field(default=0, ge=0)


class DailyOpsDigestLLMSummary(BaseModel):
    """LLM activity counts and Decimal-only estimated spend."""

    model_config = {"frozen": True}

    llm_calls: int = Field(default=0, ge=0)
    budget_blocks: int = Field(default=0, ge=0)
    cooldown_blocks: int = Field(default=0, ge=0)
    provider_failures: int = Field(default=0, ge=0)
    estimated_spend_usd: Optional[Decimal] = Field(
        default=None,
        description=(
            "Sum of Decimal-backed provider cost evidence. None when no "
            "cost data exists; never fabricated zero."
        ),
    )

    @field_validator("estimated_spend_usd", mode="before")
    @classmethod
    def _reject_float_in_spend(cls, value: object) -> Optional[Decimal]:
        if value is None:
            return None
        if isinstance(value, float):
            raise ValueError("Float financial values are forbidden; use Decimal")
        if isinstance(value, Decimal):
            return value
        return Decimal(str(value))


class DailyOpsDigestPnLSummary(BaseModel):
    """Paper PnL summary derived from repository-backed Decimal columns."""

    model_config = {"frozen": True}

    realized_pnl: Optional[Decimal] = Field(
        default=None,
        description="Sum of repository-backed realized PnL within window.",
    )
    unrealized_pnl: Optional[Decimal] = Field(
        default=None,
        description="Sum of repository-backed unrealized PnL; None when not derivable.",
    )
    gas_and_fees: Optional[Decimal] = Field(
        default=None,
        description="Sum of repository-backed gas + CLOB fees within window.",
    )
    closed_position_count: int = Field(default=0, ge=0)
    open_position_count: int = Field(default=0, ge=0)

    @field_validator(
        "realized_pnl", "unrealized_pnl", "gas_and_fees", mode="before"
    )
    @classmethod
    def _reject_float_in_pnl(cls, value: object) -> Optional[Decimal]:
        if value is None:
            return None
        if isinstance(value, float):
            raise ValueError("Float financial values are forbidden; use Decimal")
        if isinstance(value, Decimal):
            return value
        return Decimal(str(value))


class DailyOpsDigestEventHighlight(BaseModel):
    """One typed, secret-safe operational event highlight for the digest."""

    model_config = {"frozen": True}

    event_id: str = Field(..., min_length=1, max_length=64)
    event_type: OperationalEventType
    severity: OperationalEventSeverity
    reason_code: OperationalEventReasonCode
    summary: str = Field(..., min_length=1, max_length=1024)
    timestamp_utc: datetime = Field(..., description="UTC event timestamp.")

    @field_validator("summary")
    @classmethod
    def _scan_summary_for_forbidden(cls, value: str) -> str:
        violations = _scan_event_payload(value)
        if violations:
            raise ValueError(f"summary contains forbidden content: {violations}")
        return value

    @field_validator("timestamp_utc")
    @classmethod
    def _require_tzaware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamp_utc must be timezone-aware")
        return value.astimezone(timezone.utc)


class DailyOpsDigestOperatorCheck(BaseModel):
    """Deterministic next-action recommendation for the operator."""

    model_config = {"frozen": True}

    category: str = Field(..., min_length=1, max_length=32)
    message: str = Field(..., min_length=1, max_length=512)
    severity: OperationalEventSeverity

    @field_validator("category")
    @classmethod
    def _validate_category(cls, value: str) -> str:
        allowed = {
            "lifecycle",
            "readiness",
            "websocket",
            "llm",
            "budget",
            "cooldown",
            "provider",
            "decision",
            "execution",
            "circuit_breaker",
            "general",
        }
        if value not in allowed:
            raise ValueError(
                f"category must be one of {sorted(allowed)}; got {value!r}"
            )
        return value

    @field_validator("message")
    @classmethod
    def _scan_message_for_forbidden(cls, value: str) -> str:
        violations = _scan_event_payload(value)
        if violations:
            raise ValueError(f"message contains forbidden content: {violations}")
        return value


class DailyOpsDigestTelegramSummary(BaseModel):
    """Short, secret-safe Telegram digest summary.

    ``text`` is deterministic, secret-safe, and bounded. When ``enabled``
    is False the summary is treated as not-applicable and never sent.
    """

    model_config = {"frozen": True}

    enabled: bool = Field(default=False)
    text: Optional[str] = Field(default=None, max_length=1024)

    @field_validator("text")
    @classmethod
    def _scan_text_for_forbidden(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        violations = _scan_event_payload(value)
        if violations:
            raise ValueError(f"text contains forbidden content: {violations}")
        return value


class DailyOpsDigestTelegramResult(BaseModel):
    """Typed outcome of optional Telegram digest delivery."""

    model_config = {"frozen": True}

    status: Literal["sent", "disabled", "skipped", "failed"] = Field(
        default="disabled",
        description="sent/disabled/skipped/failed delivery status.",
    )
    sent_at_utc: Optional[datetime] = None
    failure_reason: Optional[str] = Field(default=None, max_length=128)

    @field_validator("sent_at_utc")
    @classmethod
    def _validate_tzaware(cls, value: Optional[datetime]) -> Optional[datetime]:
        if value is None:
            return value
        if value.tzinfo is None:
            raise ValueError("sent_at_utc must be timezone-aware")
        return value.astimezone(timezone.utc)

    @field_validator("failure_reason")
    @classmethod
    def _scan_failure_reason(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        violations = _scan_event_payload(value)
        if violations:
            raise ValueError(
                f"failure_reason contains forbidden content: {violations}"
            )
        return value


class DailyOpsDigestWriteResult(BaseModel):
    """Typed outcome of writing the digest file."""

    model_config = {"frozen": True}

    path: str = Field(..., min_length=1, max_length=512)
    written: bool
    bytes_written: int = Field(default=0, ge=0)


class DailyOpsDigestReport(BaseModel):
    """Top-level typed report for a single daily ops digest invocation."""

    model_config = {"frozen": True}

    status: DailyOpsDigestStatus
    request: DailyOpsDigestRequest
    run_summary: Optional[DailyOpsDigestRunSummary] = None
    decision_summary: Optional[DailyOpsDigestDecisionSummary] = None
    llm_summary: Optional[DailyOpsDigestLLMSummary] = None
    pnl_summary: Optional[DailyOpsDigestPnLSummary] = None
    top_events: list[DailyOpsDigestEventHighlight] = Field(default_factory=list)
    unresolved_warnings: list[DailyOpsDigestEventHighlight] = Field(
        default_factory=list
    )
    unresolved_errors: list[DailyOpsDigestEventHighlight] = Field(
        default_factory=list
    )
    operator_checks: list[DailyOpsDigestOperatorCheck] = Field(default_factory=list)
    telegram_result: DailyOpsDigestTelegramResult
    write_result: DailyOpsDigestWriteResult
    failure_reason: Optional[DailyOpsDigestFailureReason] = None
    message: Optional[str] = Field(default=None, max_length=256)

    @field_validator("message")
    @classmethod
    def _scan_message_for_forbidden(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        violations = _scan_event_payload(value)
        if violations:
            raise ValueError(f"message contains forbidden content: {violations}")
        return value

    @model_validator(mode="after")
    def _validate_status_consistency(self) -> "DailyOpsDigestReport":
        non_success_failures = {
            DailyOpsDigestStatus.DATABASE_UNAVAILABLE,
            DailyOpsDigestStatus.MISSING_TABLE,
            DailyOpsDigestStatus.REPOSITORY_FAILURE,
            DailyOpsDigestStatus.PATH_FAILURE,
            DailyOpsDigestStatus.FORBIDDEN_CONTENT,
            DailyOpsDigestStatus.INVALID_REQUEST,
            DailyOpsDigestStatus.READ_CAP_REACHED,
        }
        if self.status in non_success_failures and self.failure_reason is None:
            raise ValueError(
                f"status {self.status.value} requires a failure_reason"
            )
        if self.status == DailyOpsDigestStatus.SUCCESS and self.run_summary is None:
            raise ValueError(
                "status SUCCESS requires a run_summary derived from typed events; "
                "use EMPTY_WINDOW for zero-event days"
            )
        return self
