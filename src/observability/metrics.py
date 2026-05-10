"""
src/observability/metrics.py

Typed Prometheus metrics schemas and thread-safe registry for WI-47.

Defines MetricType, MetricLabelSet, MetricSample, MetricsSnapshot, event
schemas (DecisionMetricEvent, ExecutionMetricEvent, LatencyMetricEvent,
BacktestReadinessMetric), and a lock-protected MetricsRegistry that
renders valid Prometheus text exposition format.

All financial/Decimal paths are enforced. Labels are validated for
low-cardinality constraints. No secrets or high-cardinality identifiers
are accepted.
"""

from __future__ import annotations

import asyncio
import time as _time
from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator

from src.schemas.execution import ExecutionAction

# ── Forbidden label keys ──────────────────────────────────────────────────

_FORBIDDEN_LABEL_KEYS: frozenset[str] = frozenset(
    {
        "condition_id",
        "token_id",
        "wallet_address",
        "wallet",
        "private_key",
        "api_key",
        "prompt_text",
        "prompt",
        "reasoning_text",
        "reasoning",
        "exception_message",
        "error_message",
        "secret",
        "address",
    }
)

_FORBIDDEN_LABEL_VALUE_PATTERNS: tuple[str, ...] = (
    "0x",
    "sk-",
    "pk-",
    "api-",
)


# ── Enums ─────────────────────────────────────────────────────────────────


class MetricType(str, Enum):
    """Prometheus metric type — COUNTER or GAUGE."""

    COUNTER = "COUNTER"
    GAUGE = "GAUGE"


class DecisionLabel(str, Enum):
    """Low-cardinality decision action labels."""

    BUY = "BUY"
    HOLD = "HOLD"
    SKIP = "SKIP"


class BacktestVerdictLabel(str, Enum):
    """Low-cardinality backtest live-readiness verdict labels."""

    PASS = "PASS"
    FAIL_NEGATIVE_PNL = "FAIL_NEGATIVE_PNL"
    FAIL_DRAWDOWN = "FAIL_DRAWDOWN"
    FAIL_INSUFFICIENT_TRADES = "FAIL_INSUFFICIENT_TRADES"
    FAIL_WEAK_CALIBRATION = "FAIL_WEAK_CALIBRATION"
    FAIL_DATA_QUALITY = "FAIL_DATA_QUALITY"
    UNKNOWN = "UNKNOWN"


# ── Schemas ───────────────────────────────────────────────────────────────


class MetricLabelSet(BaseModel):
    """Validated low-cardinality label set for a metric sample.

    Rejects high-cardinality identifiers (condition_id, token_id, wallet
    addresses) and secret-like values at the model boundary.
    """

    labels: dict[str, str] = Field(default_factory=dict)

    @field_validator("labels")
    @classmethod
    def _reject_forbidden_keys(cls, v: dict[str, str]) -> dict[str, str]:
        for key in v:
            if key.lower() in _FORBIDDEN_LABEL_KEYS:
                raise ValueError(
                    f"Forbidden high-cardinality label key: '{key}'"
                )
        return v

    @field_validator("labels")
    @classmethod
    def _reject_secret_values(cls, v: dict[str, str]) -> dict[str, str]:
        for _key, value in v.items():
            lower_val = value.lower()
            for pattern in _FORBIDDEN_LABEL_VALUE_PATTERNS:
                if lower_val.startswith(pattern):
                    raise ValueError(
                        "Forbidden label value pattern detected"
                    )
        return v


class MetricSample(BaseModel):
    """A single Prometheus metric sample — name, type, help, labels, value.

    ``value`` is always Decimal — floating-point arithmetic is forbidden.
    """

    name: str = Field(..., min_length=1)
    help_text: str = Field(..., min_length=1)
    type: MetricType
    labels: MetricLabelSet = Field(default_factory=MetricLabelSet)
    value: Decimal

    @field_validator("value", mode="before")
    @classmethod
    def _reject_float(cls, v: object) -> Decimal:
        if isinstance(v, float):
            raise ValueError("Float values are forbidden; use Decimal")
        if isinstance(v, Decimal):
            return v
        try:
            return Decimal(str(v))
        except Exception:
            raise ValueError(f"Cannot coerce to Decimal: {type(v).__name__}")


class MetricsSnapshot(BaseModel):
    """Immutable snapshot of the current metric registry state."""

    samples: list[MetricSample] = Field(default_factory=list)
    snapshot_at_utc: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class DecisionMetricEvent(BaseModel):
    """Emitted when an evaluation produces a BUY / HOLD / SKIP decision."""

    decision: DecisionLabel

    @field_validator("decision", mode="before")
    @classmethod
    def _normalize_decision(cls, v: object) -> DecisionLabel:
        if isinstance(v, DecisionLabel):
            return v
        if isinstance(v, str):
            try:
                return DecisionLabel(v.upper())
            except ValueError:
                raise ValueError(
                    f"Unknown decision action: '{v}'. "
                    f"Must be one of {[d.value for d in DecisionLabel]}"
                )
        raise ValueError(
            f"Cannot coerce to DecisionLabel: {type(v).__name__}"
        )


class ExecutionMetricEvent(BaseModel):
    """Emitted when ExecutionRouter produces a result."""

    action: ExecutionAction

    @field_validator("action", mode="before")
    @classmethod
    def _normalize_action(cls, v: object) -> ExecutionAction:
        if isinstance(v, ExecutionAction):
            return v
        if isinstance(v, str):
            try:
                return ExecutionAction(v.upper())
            except ValueError:
                raise ValueError(
                    f"Unknown execution action: '{v}'. "
                    f"Must be one of {[a.value for a in ExecutionAction]}"
                )
        raise ValueError(
            f"Cannot coerce to ExecutionAction: {type(v).__name__}"
        )


class LatencyMetricEvent(BaseModel):
    """Emitted for layer latency measurements (evaluation, context, routing)."""

    layer: str = Field(..., min_length=1)
    value_seconds: Decimal = Field(..., ge=0)

    @field_validator("layer")
    @classmethod
    def _normalize_layer(cls, v: str) -> str:
        stripped = v.strip().lower()
        if not stripped:
            raise ValueError("Layer must not be empty")
        # Reject high-cardinality layer names
        if stripped in _FORBIDDEN_LABEL_KEYS:
            raise ValueError(f"Forbidden layer name: '{v}'")
        return stripped

    @field_validator("value_seconds", mode="before")
    @classmethod
    def _reject_float_seconds(cls, v: object) -> Decimal:
        if isinstance(v, float):
            raise ValueError("Float values are forbidden; use Decimal")
        if isinstance(v, Decimal):
            return v
        try:
            return Decimal(str(v))
        except Exception:
            raise ValueError(f"Cannot coerce to Decimal: {type(v).__name__}")


class BacktestReadinessMetric(BaseModel):
    """Gauge representing the latest WI-44 live-readiness verdict."""

    verdict: BacktestVerdictLabel = Field(
        default=BacktestVerdictLabel.UNKNOWN
    )


# ── Metrics Registry ──────────────────────────────────────────────────────


class MetricsRegistry:
    """Thread-safe, lock-protected Prometheus metric registry.

    Uses ``asyncio.Lock`` to guard concurrent counter/gauges mutations
    and produce consistent snapshots.  All numeric values are ``Decimal``.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._counters: dict[str, dict[str, Decimal]] = {}
        self._gauges: dict[str, dict[str, Decimal]] = {}
        self._counter_helps: dict[str, str] = {}
        self._gauge_helps: dict[str, str] = {}
        self._last_heartbeat_at_utc: Optional[datetime] = None
        # Track decisions per hour window
        self._decision_timestamps: list[float] = []

        self._init_required_metrics()

    def _init_required_metrics(self) -> None:
        """Seed the registry with all required metric declarations."""
        # Counters
        for name, help_text in [
            (
                "poly_agent_decisions_total",
                "Total evaluation decisions by action (BUY/HOLD/SKIP)",
            ),
            (
                "poly_agent_executions_total",
                "Total execution routing results by ExecutionAction",
            ),
            (
                "poly_agent_ws_reconnects_total",
                "Total WebSocket reconnection attempts",
            ),
            (
                "poly_agent_ws_errors_total",
                "Total WebSocket errors encountered",
            ),
        ]:
            self._counters[name] = {}
            self._counter_helps[name] = help_text

        # Gauges
        for name, help_text in [
            (
                "poly_agent_heartbeat_age_seconds",
                "Seconds since last WebSocket heartbeat received",
            ),
            (
                "poly_agent_active_market_count",
                "Number of currently active subscribed markets",
            ),
            (
                "poly_agent_backtest_readiness_verdict",
                "Latest WI-44 live-readiness verdict (1=known, 0=unknown/not_available)",
            ),
            (
                "poly_agent_decisions_per_hour",
                "Decision rate — decisions in the trailing 60-minute window",
            ),
        ]:
            self._gauges[name] = {}
            self._gauge_helps[name] = help_text

        # Latency counters (as cumulative sum + count for avg)
        for prefix, help_text in [
            ("poly_agent_evaluation_latency", "Evaluation latency"),
            ("poly_agent_context_build_latency", "Context build latency"),
            (
                "poly_agent_execution_routing_latency",
                "Execution routing latency",
            ),
        ]:
            name_total = f"{prefix}_seconds_total"
            name_count = f"{prefix}_count"
            self._counters[name_total] = {}
            self._counter_helps[name_total] = f"{help_text} — cumulative seconds"
            self._counters[name_count] = {}
            self._counter_helps[name_count] = f"{help_text} — call count"

        # WI-52: LLM cost guard metrics
        for name, help_text in [
            (
                "poly_agent_llm_calls_total",
                "Total LLM provider calls by type (primary/reflection)",
            ),
            (
                "poly_agent_llm_budget_blocks_total",
                "Total LLM budget blocks by reason",
            ),
            (
                "poly_agent_llm_cooldown_blocks_total",
                "Total per-market cognitive cooldown blocks",
            ),
            (
                "poly_agent_llm_tokens_total",
                "Total tokens consumed by LLM calls (input + output)",
            ),
            (
                "poly_agent_llm_estimated_spend_usd_total",
                "Total estimated LLM spend in USD",
            ),
        ]:
            self._counters[name] = {}
            self._counter_helps[name] = help_text

        self._gauges["poly_agent_active_cooldown_count"] = {}
        self._gauge_helps["poly_agent_active_cooldown_count"] = (
            "Number of markets currently in LLM cooldown"
        )

    # ── Public mutation API ────────────────────────────────────────────

    async def record_decision(self, event: DecisionMetricEvent) -> None:
        """Increment the decision counter for the given decision label."""
        label_key = _serialize_labels({"decision": event.decision.value})
        async with self._lock:
            counter = self._counters["poly_agent_decisions_total"]
            current = counter.get(label_key, Decimal("0"))
            counter[label_key] = current + Decimal("1")
            self._decision_timestamps.append(_time.monotonic())
            # Prune old timestamps (> 1 hour)
            cutoff = _time.monotonic() - 3600
            self._decision_timestamps = [
                ts for ts in self._decision_timestamps if ts > cutoff
            ]
            # Update decisions per hour gauge
            self._gauges["poly_agent_decisions_per_hour"][""] = Decimal(
                str(len(self._decision_timestamps))
            )

    async def record_execution(self, event: ExecutionMetricEvent) -> None:
        """Increment the execution counter for the given ExecutionAction."""
        label_key = _serialize_labels({"action": event.action.value})
        async with self._lock:
            counter = self._counters["poly_agent_executions_total"]
            current = counter.get(label_key, Decimal("0"))
            counter[label_key] = current + Decimal("1")

    async def record_latency(self, event: LatencyMetricEvent) -> None:
        """Add a latency observation to the cumulative counter."""
        safe_layer = event.layer.replace("-", "_")
        name_total = f"poly_agent_{safe_layer}_latency_seconds_total"
        name_count = f"poly_agent_{safe_layer}_latency_count"

        async with self._lock:
            if name_total in self._counters:
                total_counter = self._counters[name_total]
                total_counter[""] = total_counter.get("", Decimal("0")) + event.value_seconds
                count_counter = self._counters[name_count]
                count_counter[""] = count_counter.get("", Decimal("0")) + Decimal("1")

    async def set_ws_reconnect_count(self, count: int) -> None:
        """Set the WS reconnect counter to an absolute value."""
        async with self._lock:
            self._counters["poly_agent_ws_reconnects_total"][""] = Decimal(
                str(max(0, count))
            )

    async def set_ws_error_count(self, count: int) -> None:
        """Set the WS error counter to an absolute value."""
        async with self._lock:
            self._counters["poly_agent_ws_errors_total"][""] = Decimal(
                str(max(0, count))
            )

    async def set_heartbeat_age(self, age_seconds: Decimal) -> None:
        """Set the last heartbeat age gauge."""
        async with self._lock:
            self._last_heartbeat_at_utc = datetime.now(timezone.utc)
            self._gauges["poly_agent_heartbeat_age_seconds"][""] = age_seconds

    async def set_active_market_count(self, count: int) -> None:
        """Set the active subscribed market count gauge."""
        async with self._lock:
            self._gauges["poly_agent_active_market_count"][""] = Decimal(
                str(max(0, count))
            )

    async def set_backtest_verdict(
        self, verdict: BacktestReadinessMetric | BacktestVerdictLabel | str
    ) -> None:
        """Set the backtest live-readiness verdict gauge."""
        if isinstance(verdict, BacktestReadinessMetric):
            label = verdict.verdict
        elif isinstance(verdict, BacktestVerdictLabel):
            label = verdict
        else:
            label = BacktestVerdictLabel(str(verdict).upper())
        async with self._lock:
            self._gauges["poly_agent_backtest_readiness_verdict"] = {
                _serialize_labels({"verdict": label.value}): Decimal("1")
            }

    async def update_from_ws_health(
        self,
        reconnect_count: int,
        error_count: int,
        heartbeat_age_seconds: Decimal,
        active_asset_count: int,
    ) -> None:
        """Bulk-update WebSocket-derived metrics."""
        async with self._lock:
            self._counters["poly_agent_ws_reconnects_total"][""] = Decimal(
                str(max(0, reconnect_count))
            )
            self._counters["poly_agent_ws_errors_total"][""] = Decimal(
                str(max(0, error_count))
            )
            self._gauges["poly_agent_heartbeat_age_seconds"][""] = heartbeat_age_seconds
            self._gauges["poly_agent_active_market_count"][""] = Decimal(
                str(max(0, active_asset_count))
            )
            self._last_heartbeat_at_utc = datetime.now(timezone.utc)

    # ── WI-52: LLM cost guard metrics ────────────────────────────────

    async def record_llm_call(self, *, call_type: str) -> None:
        """Increment LLM call counter by type."""
        label_key = _serialize_labels({"call_type": call_type})
        async with self._lock:
            counter = self._counters["poly_agent_llm_calls_total"]
            current = counter.get(label_key, Decimal("0"))
            counter[label_key] = current + Decimal("1")

    async def record_llm_budget_block(self, *, reason: str) -> None:
        """Increment budget block counter by reason."""
        label_key = _serialize_labels({"reason": reason})
        async with self._lock:
            counter = self._counters["poly_agent_llm_budget_blocks_total"]
            current = counter.get(label_key, Decimal("0"))
            counter[label_key] = current + Decimal("1")

    async def record_llm_cooldown_block(self) -> None:
        """Increment per-market cooldown block counter."""
        async with self._lock:
            counter = self._counters["poly_agent_llm_cooldown_blocks_total"]
            current = counter.get("", Decimal("0"))
            counter[""] = current + Decimal("1")

    async def record_llm_tokens(self, *, total_tokens: int) -> None:
        """Add to total token consumption counter."""
        async with self._lock:
            counter = self._counters["poly_agent_llm_tokens_total"]
            current = counter.get("", Decimal("0"))
            counter[""] = current + Decimal(str(total_tokens))

    async def record_llm_estimated_spend(self, *, cost_usd: Decimal) -> None:
        """Add to estimated spend counter."""
        async with self._lock:
            counter = self._counters["poly_agent_llm_estimated_spend_usd_total"]
            current = counter.get("", Decimal("0"))
            counter[""] = current + cost_usd

    async def set_active_cooldown_count(self, count: int) -> None:
        """Set the active cooldown count gauge."""
        async with self._lock:
            self._gauges["poly_agent_active_cooldown_count"][""] = Decimal(
                str(max(0, count))
            )

    # ── Read API ───────────────────────────────────────────────────────

    async def snapshot(self) -> MetricsSnapshot:
        """Return a consistent snapshot of all current metrics."""
        async with self._lock:
            samples: list[MetricSample] = []

            for name, label_map in self._counters.items():
                help_text = self._counter_helps.get(name, name)
                if not label_map:
                    samples.append(
                        MetricSample(
                            name=name,
                            help_text=help_text,
                            type=MetricType.COUNTER,
                            value=Decimal("0"),
                        )
                    )
                else:
                    for label_key, value in label_map.items():
                        labels_dict = _deserialize_labels(label_key)
                        samples.append(
                            MetricSample(
                                name=name,
                                help_text=help_text,
                                type=MetricType.COUNTER,
                                labels=MetricLabelSet(labels=labels_dict),
                                value=value,
                            )
                        )

            for name, label_map in self._gauges.items():
                help_text = self._gauge_helps.get(name, name)
                if not label_map:
                    # Emit zero for gauges never set (so all required metrics appear)
                    if name == "poly_agent_backtest_readiness_verdict":
                        samples.append(
                            MetricSample(
                                name=name,
                                help_text=help_text,
                                type=MetricType.GAUGE,
                                labels=MetricLabelSet(
                                    labels={"verdict": "UNKNOWN"}
                                ),
                                value=Decimal("0"),
                            )
                        )
                    else:
                        samples.append(
                            MetricSample(
                                name=name,
                                help_text=help_text,
                                type=MetricType.GAUGE,
                                value=Decimal("0"),
                            )
                        )
                    continue
                for label_key, value in label_map.items():
                    labels_dict = _deserialize_labels(label_key)
                    samples.append(
                        MetricSample(
                            name=name,
                            help_text=help_text,
                            type=MetricType.GAUGE,
                            labels=MetricLabelSet(labels=labels_dict),
                            value=value,
                        )
                    )

            return MetricsSnapshot(samples=samples)

    def render_prometheus(self, snapshot: MetricsSnapshot) -> str:
        """Render a MetricsSnapshot to Prometheus text exposition format.

        Output is deterministic: samples are sorted by name then label key
        for stable, testable output.
        """
        lines: list[str] = []
        seen_metric_helps: set[str] = set()

        # Sort for deterministic output
        sorted_samples = sorted(
            snapshot.samples,
            key=lambda s: (s.name, _serialize_labels(s.labels.labels)),
        )

        for sample in sorted_samples:
            # HELP and TYPE lines — once per metric name
            if sample.name not in seen_metric_helps:
                lines.append(f"# HELP {sample.name} {sample.help_text}")
                sample_type = (
                    "counter" if sample.type == MetricType.COUNTER else "gauge"
                )
                lines.append(f"# TYPE {sample.name} {sample_type}")
                seen_metric_helps.add(sample.name)

            # Metric line
            metric_line = _format_metric_line(
                sample.name, sample.labels.labels, sample.value
            )
            lines.append(metric_line)

        # Final newline per Prometheus convention
        if lines:
            lines.append("")
        return "\n".join(lines)


# ── Helpers ───────────────────────────────────────────────────────────────


def _serialize_labels(labels: Mapping[str, str]) -> str:
    """Serialize a label dict to a stable string key for dict storage."""
    items = sorted(labels.items())
    return ",".join(f"{k}={v}" for k, v in items)


def _deserialize_labels(label_key: str) -> dict[str, str]:
    """Deserialize a label key back into a dict."""
    if not label_key:
        return {}
    pairs = label_key.split(",")
    result: dict[str, str] = {}
    for pair in pairs:
        if "=" in pair:
            k, v = pair.split("=", 1)
            result[k] = v
    return result


def _format_metric_line(
    name: str, labels: dict[str, str], value: Decimal
) -> str:
    """Format a single Prometheus metric line."""
    if labels:
        label_parts = ",".join(
            f'{k}="{v}"' for k, v in sorted(labels.items())
        )
        label_str = f"{{{label_parts}}}"
    else:
        label_str = ""
    return f"{name}{label_str} {value}"
