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
