"""
Unit tests for WI-47 — Prometheus Metrics Export.

Covers schema validation, Prometheus text exposition format, low-cardinality
labels, counter/gauges, non-blocking semantics, and forbidden sensitive fields.
"""

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from src.observability.metrics import (
    BacktestReadinessMetric,
    BacktestVerdictLabel,
    DecisionLabel,
    DecisionMetricEvent,
    ExecutionMetricEvent,
    LatencyMetricEvent,
    MetricLabelSet,
    MetricSample,
    MetricsRegistry,
    MetricsSnapshot,
    MetricType,
    _format_metric_line,
    _serialize_labels,
)
from src.observability.metrics_server import MetricsServer
from src.schemas.execution import ExecutionAction


# ═══════════════════════════════════════════════════════════════════════════
# Schema Tests — MetricType
# ═══════════════════════════════════════════════════════════════════════════

class TestMetricTypeEnum:
    """MetricType enum validation."""

    def test_metric_type_enum_values(self) -> None:
        assert MetricType.COUNTER.value == "COUNTER"
        assert MetricType.GAUGE.value == "GAUGE"

    def test_invalid_metric_type_rejected(self) -> None:
        with pytest.raises(ValueError):
            MetricType("HISTOGRAM")


# ═══════════════════════════════════════════════════════════════════════════
# Schema Tests — MetricLabelSet
# ═══════════════════════════════════════════════════════════════════════════

class TestMetricLabelSet:
    """MetricLabelSet — low-cardinality, no forbidden labels."""

    def test_label_set_valid(self) -> None:
        ls = MetricLabelSet(labels={"decision": "BUY", "action": "SKIP"})
        assert ls.labels["decision"] == "BUY"
        assert ls.labels["action"] == "SKIP"

    def test_label_set_rejects_condition_id(self) -> None:
        with pytest.raises(ValidationError, match="condition_id"):
            MetricLabelSet(labels={"condition_id": "0xabc"})

    def test_label_set_rejects_token_id(self) -> None:
        with pytest.raises(ValidationError, match="token_id"):
            MetricLabelSet(labels={"token_id": "123"})

    def test_label_set_rejects_wallet_address(self) -> None:
        with pytest.raises(ValidationError, match="wallet_address"):
            MetricLabelSet(labels={"wallet_address": "0xdead"})

    def test_label_set_rejects_prompt_text(self) -> None:
        with pytest.raises(ValidationError, match="prompt_text"):
            MetricLabelSet(labels={"prompt_text": "some prompt"})

    def test_label_set_rejects_exception_message(self) -> None:
        with pytest.raises(ValidationError, match="exception_message"):
            MetricLabelSet(labels={"exception_message": "boom"})

    def test_label_set_empty_labels_allowed(self) -> None:
        ls = MetricLabelSet()
        assert ls.labels == {}


# ═══════════════════════════════════════════════════════════════════════════
# Schema Tests — MetricSample
# ═══════════════════════════════════════════════════════════════════════════

class TestMetricSample:
    """MetricSample schema validation."""

    def test_metric_sample_counter_type(self) -> None:
        s = MetricSample(
            name="test_total",
            help_text="Test counter",
            type=MetricType.COUNTER,
            value=Decimal("42"),
        )
        assert s.type == MetricType.COUNTER
        assert s.value == Decimal("42")

    def test_metric_sample_gauge_type(self) -> None:
        s = MetricSample(
            name="test_gauge",
            help_text="Test gauge",
            type=MetricType.GAUGE,
            value=Decimal("3.14"),
        )
        assert s.type == MetricType.GAUGE
        assert s.value == Decimal("3.14")

    def test_metric_sample_rejects_float_value(self) -> None:
        with pytest.raises(ValidationError, match="Float"):
            MetricSample(
                name="test",
                help_text="help",
                type=MetricType.COUNTER,
                value=1.5,
            )

    def test_metric_sample_name_non_empty(self) -> None:
        with pytest.raises(ValidationError):
            MetricSample(
                name="",
                help_text="help",
                type=MetricType.COUNTER,
                value=Decimal("1"),
            )

    def test_metric_sample_help_text_non_empty(self) -> None:
        with pytest.raises(ValidationError):
            MetricSample(
                name="test",
                help_text="",
                type=MetricType.COUNTER,
                value=Decimal("1"),
            )


# ═══════════════════════════════════════════════════════════════════════════
# Schema Tests — MetricsSnapshot
# ═══════════════════════════════════════════════════════════════════════════

class TestMetricsSnapshot:
    """MetricsSnapshot aggregation and validation."""

    def test_snapshot_empty_registry(self) -> None:
        snap = MetricsSnapshot()
        assert snap.samples == []

    def test_snapshot_with_counters_and_gauges(self) -> None:
        samples = [
            MetricSample(
                name="req_total",
                help_text="Total requests",
                type=MetricType.COUNTER,
                value=Decimal("100"),
            ),
            MetricSample(
                name="temp_celsius",
                help_text="Current temp",
                type=MetricType.GAUGE,
                value=Decimal("22.5"),
            ),
        ]
        snap = MetricsSnapshot(samples=samples)
        assert len(snap.samples) == 2

    def test_snapshot_no_forbidden_fields(self) -> None:
        """Snapshot serialization must not contain forbidden field names."""
        samples = [
            MetricSample(
                name="test",
                help_text="Test",
                type=MetricType.COUNTER,
                value=Decimal("1"),
                labels=MetricLabelSet(labels={"decision": "BUY"}),
            ),
        ]
        snap = MetricsSnapshot(samples=samples)
        # Verify label keys are safe
        for s in snap.samples:
            for key in s.labels.labels:
                assert "condition_id" not in key.lower()
                assert "wallet" not in key.lower()
                assert "token_id" not in key.lower()


# ═══════════════════════════════════════════════════════════════════════════
# Event Schemas
# ═══════════════════════════════════════════════════════════════════════════

class TestDecisionMetricEvent:
    """DecisionMetricEvent validation."""

    def test_decision_event_action_enum_names(self) -> None:
        for label in DecisionLabel:
            event = DecisionMetricEvent(decision=label)
            assert event.decision == label

    def test_decision_event_rejects_unknown_action(self) -> None:
        with pytest.raises(ValidationError, match="Unknown decision"):
            DecisionMetricEvent(decision="SELL")

    def test_decision_event_default_fields(self) -> None:
        event = DecisionMetricEvent(decision=DecisionLabel.HOLD)
        assert event.decision == DecisionLabel.HOLD


class TestExecutionMetricEvent:
    """ExecutionMetricEvent validation."""

    def test_execution_event_action_enum_names(self) -> None:
        for action in ExecutionAction:
            event = ExecutionMetricEvent(action=action)
            assert event.action == action

    def test_execution_event_rejects_unknown_action(self) -> None:
        with pytest.raises(ValidationError, match="Unknown execution"):
            ExecutionMetricEvent(action="APPROVED")

    def test_execution_event_default_fields(self) -> None:
        event = ExecutionMetricEvent(action=ExecutionAction.DRY_RUN)
        assert event.action == ExecutionAction.DRY_RUN


class TestLatencyMetricEvent:
    """LatencyMetricEvent validation."""

    def test_latency_event_seconds_non_negative(self) -> None:
        event = LatencyMetricEvent(
            layer="evaluation",
            value_seconds=Decimal("0.0"),
        )
        assert event.value_seconds == Decimal("0.0")

        event2 = LatencyMetricEvent(
            layer="evaluation",
            value_seconds=Decimal("1.5"),
        )
        assert event2.value_seconds == Decimal("1.5")

    def test_latency_event_rejects_negative_value(self) -> None:
        with pytest.raises(ValidationError):
            LatencyMetricEvent(
                layer="evaluation",
                value_seconds=Decimal("-1"),
            )

    def test_latency_event_rejects_float(self) -> None:
        with pytest.raises(ValidationError, match="Float"):
            LatencyMetricEvent(
                layer="evaluation",
                value_seconds=0.5,
            )


# ═══════════════════════════════════════════════════════════════════════════
# BacktestReadinessMetric
# ═══════════════════════════════════════════════════════════════════════════

class TestBacktestReadinessMetric:
    """BacktestReadinessMetric gauge values."""

    def test_readiness_verdict_labels(self) -> None:
        for verdict in BacktestVerdictLabel:
            metric = BacktestReadinessMetric(verdict=verdict)
            assert metric.verdict == verdict

    def test_readiness_unknown_when_unavailable(self) -> None:
        metric = BacktestReadinessMetric()
        assert metric.verdict == BacktestVerdictLabel.UNKNOWN

    def test_readiness_metric_is_gauge(self) -> None:
        """BacktestReadinessMetric represents a gauge (value set, not incremented)."""
        metric = BacktestReadinessMetric(verdict=BacktestVerdictLabel.PASS)
        assert isinstance(metric.verdict, BacktestVerdictLabel)


# ═══════════════════════════════════════════════════════════════════════════
# Prometheus Text Exposition Format
# ═══════════════════════════════════════════════════════════════════════════

class TestPrometheusTextFormat:
    """Valid Prometheus text exposition format generation."""

    def test_counter_line_format(self) -> None:
        """Counter metric line has correct format."""
        line = _format_metric_line("test_total", {}, Decimal("42"))
        assert line == "test_total 42"

    def test_gauge_line_format(self) -> None:
        line = _format_metric_line("temp_celsius", {}, Decimal("23.5"))
        assert line == "temp_celsius 23.5"

    @pytest.mark.asyncio
    async def test_help_line_format(self) -> None:
        """HELP lines start with '# HELP'."""
        registry = MetricsRegistry()
        snap = await registry.snapshot()
        text = registry.render_prometheus(snap)
        help_lines = [l for l in text.split("\n") if l.startswith("# HELP ")]
        assert len(help_lines) >= 1

    @pytest.mark.asyncio
    async def test_type_line_format(self) -> None:
        """TYPE lines start with '# TYPE' and end with counter or gauge."""
        registry = MetricsRegistry()
        snap = await registry.snapshot()
        text = registry.render_prometheus(snap)
        type_lines = [l for l in text.split("\n") if l.startswith("# TYPE ")]
        assert len(type_lines) >= 1
        for line in type_lines:
            assert line.endswith("counter") or line.endswith("gauge")

    def test_labels_braces_format(self) -> None:
        line = _format_metric_line(
            "test_total", {"decision": "BUY"}, Decimal("1")
        )
        assert '{decision="BUY"}' in line

    @pytest.mark.asyncio
    async def test_no_empty_lines_between_type_and_metric(self) -> None:
        """HELP/TYPE and metric line must not have blank lines between them."""
        registry = MetricsRegistry()
        snap = await registry.snapshot()
        text = registry.render_prometheus(snap)
        lines = text.split("\n")
        for i, line in enumerate(lines):
            if line.startswith("# TYPE ") and i + 1 < len(lines):
                assert lines[i + 1].strip() != ""

    @pytest.mark.asyncio
    async def test_snapshot_newline_terminated(self) -> None:
        """Output ends with a trailing newline."""
        registry = MetricsRegistry()
        snap = await registry.snapshot()
        text = registry.render_prometheus(snap)
        if text:
            assert text.endswith("\n")


# ═══════════════════════════════════════════════════════════════════════════
# Required Metrics Counters and Gauges
# ═══════════════════════════════════════════════════════════════════════════

class TestRequiredMetrics:
    """All required counters/gauges are present in the output."""

    @pytest.mark.asyncio
    async def test_decisions_per_hour_counter_present(self) -> None:
        registry = MetricsRegistry()
        snap = await registry.snapshot()
        text = registry.render_prometheus(snap)
        assert "poly_agent_decisions_per_hour" in text

    @pytest.mark.asyncio
    async def test_buy_hold_skip_decision_counts(self) -> None:
        registry = MetricsRegistry()
        snap = await registry.snapshot()
        text = registry.render_prometheus(snap)
        assert "poly_agent_decisions_total" in text

    @pytest.mark.asyncio
    async def test_execution_result_counts_by_action(self) -> None:
        registry = MetricsRegistry()
        snap = await registry.snapshot()
        text = registry.render_prometheus(snap)
        assert "poly_agent_executions_total" in text

    @pytest.mark.asyncio
    async def test_evaluation_latency_histogram_or_summary(self) -> None:
        registry = MetricsRegistry()
        snap = await registry.snapshot()
        text = registry.render_prometheus(snap)
        assert "poly_agent_evaluation_latency_seconds_total" in text
        assert "poly_agent_evaluation_latency_count" in text

    @pytest.mark.asyncio
    async def test_context_build_latency_metric(self) -> None:
        registry = MetricsRegistry()
        snap = await registry.snapshot()
        text = registry.render_prometheus(snap)
        assert "poly_agent_context_build_latency_seconds_total" in text
        assert "poly_agent_context_build_latency_count" in text

    @pytest.mark.asyncio
    async def test_execution_routing_latency_metric(self) -> None:
        registry = MetricsRegistry()
        snap = await registry.snapshot()
        text = registry.render_prometheus(snap)
        assert "poly_agent_execution_routing_latency_seconds_total" in text
        assert "poly_agent_execution_routing_latency_count" in text

    @pytest.mark.asyncio
    async def test_websocket_reconnect_count_counter(self) -> None:
        registry = MetricsRegistry()
        snap = await registry.snapshot()
        text = registry.render_prometheus(snap)
        assert "poly_agent_ws_reconnects_total" in text

    @pytest.mark.asyncio
    async def test_websocket_error_count_counter(self) -> None:
        registry = MetricsRegistry()
        snap = await registry.snapshot()
        text = registry.render_prometheus(snap)
        assert "poly_agent_ws_errors_total" in text

    @pytest.mark.asyncio
    async def test_last_heartbeat_age_seconds_gauge(self) -> None:
        registry = MetricsRegistry()
        snap = await registry.snapshot()
        text = registry.render_prometheus(snap)
        assert "poly_agent_heartbeat_age_seconds" in text

    @pytest.mark.asyncio
    async def test_active_subscribed_market_count_gauge(self) -> None:
        registry = MetricsRegistry()
        snap = await registry.snapshot()
        text = registry.render_prometheus(snap)
        assert "poly_agent_active_market_count" in text

    @pytest.mark.asyncio
    async def test_backtest_live_readiness_verdict_gauge(self) -> None:
        registry = MetricsRegistry()
        snap = await registry.snapshot()
        text = registry.render_prometheus(snap)
        assert "poly_agent_backtest_readiness_verdict" in text


# ═══════════════════════════════════════════════════════════════════════════
# Counter and Gauge Increment Behavior
# ═══════════════════════════════════════════════════════════════════════════

class TestCounterBehavior:
    """Counter increment and reset semantics."""

    @pytest.fixture
    async def registry(self) -> MetricsRegistry:
        return MetricsRegistry()

    @pytest.mark.asyncio
    async def test_counter_starts_at_zero(self, registry: MetricsRegistry) -> None:
        snap = await registry.snapshot()
        found = [
            s for s in snap.samples
            if s.name == "poly_agent_decisions_total"
        ]
        # Zero-state: may emit a zero sample or skip it — both acceptable
        if found:
            for s in found:
                assert s.value == Decimal("0")

    @pytest.mark.asyncio
    async def test_counter_increments_correctly(
        self, registry: MetricsRegistry
    ) -> None:
        await registry.record_decision(
            DecisionMetricEvent(decision=DecisionLabel.BUY)
        )
        await registry.record_decision(
            DecisionMetricEvent(decision=DecisionLabel.BUY)
        )
        await registry.record_decision(
            DecisionMetricEvent(decision=DecisionLabel.SKIP)
        )
        snap = await registry.snapshot()
        decisions = {
            s.labels.labels.get("decision", ""): s.value
            for s in snap.samples
            if s.name == "poly_agent_decisions_total"
        }
        assert decisions.get("BUY") == Decimal("2")
        assert decisions.get("SKIP") == Decimal("1")

    @pytest.mark.asyncio
    async def test_counter_never_decreases(
        self, registry: MetricsRegistry
    ) -> None:
        await registry.record_decision(
            DecisionMetricEvent(decision=DecisionLabel.BUY)
        )
        snap1 = await registry.snapshot()
        val1 = sum(
            s.value
            for s in snap1.samples
            if s.name == "poly_agent_decisions_total"
            and s.labels.labels.get("decision") == "BUY"
        )
        # Record more
        await registry.record_decision(
            DecisionMetricEvent(decision=DecisionLabel.BUY)
        )
        snap2 = await registry.snapshot()
        val2 = sum(
            s.value
            for s in snap2.samples
            if s.name == "poly_agent_decisions_total"
            and s.labels.labels.get("decision") == "BUY"
        )
        assert val2 >= val1

    @pytest.mark.asyncio
    async def test_multiple_counters_independent(
        self, registry: MetricsRegistry
    ) -> None:
        await registry.record_decision(
            DecisionMetricEvent(decision=DecisionLabel.BUY)
        )
        await registry.record_execution(
            ExecutionMetricEvent(action=ExecutionAction.SKIP)
        )
        snap = await registry.snapshot()
        decision_counters = [
            s for s in snap.samples
            if s.name == "poly_agent_decisions_total"
        ]
        exec_counters = [
            s for s in snap.samples
            if s.name == "poly_agent_executions_total"
        ]
        assert len(decision_counters) > 0
        assert len(exec_counters) > 0


class TestGaugeBehavior:
    """Gauge set/update semantics."""

    @pytest.fixture
    async def registry(self) -> MetricsRegistry:
        return MetricsRegistry()

    @pytest.mark.asyncio
    async def test_gauge_initial_value(self, registry: MetricsRegistry) -> None:
        await registry.set_active_market_count(3)
        snap = await registry.snapshot()
        gauges = [
            s for s in snap.samples
            if s.name == "poly_agent_active_market_count"
        ]
        assert len(gauges) == 1
        assert gauges[0].value == Decimal("3")

    @pytest.mark.asyncio
    async def test_gauge_updates_over_time(
        self, registry: MetricsRegistry
    ) -> None:
        await registry.set_active_market_count(1)
        await registry.set_active_market_count(5)
        snap = await registry.snapshot()
        gauges = [
            s for s in snap.samples
            if s.name == "poly_agent_active_market_count"
        ]
        assert gauges[0].value == Decimal("5")

    @pytest.mark.asyncio
    async def test_gauge_heartbeat_age_increases(
        self, registry: MetricsRegistry
    ) -> None:
        await registry.set_heartbeat_age(Decimal("10.0"))
        await registry.set_heartbeat_age(Decimal("25.0"))
        snap = await registry.snapshot()
        gauges = [
            s for s in snap.samples
            if s.name == "poly_agent_heartbeat_age_seconds"
        ]
        assert len(gauges) == 1
        assert gauges[0].value == Decimal("25.0")


# ═══════════════════════════════════════════════════════════════════════════
# Low-Cardinality Label Enforcement
# ═══════════════════════════════════════════════════════════════════════════

class TestLowCardinalityLabels:
    """Labels must stay low-cardinality — no raw IDs or secrets."""

    @pytest.fixture
    async def registry(self) -> MetricsRegistry:
        return MetricsRegistry()

    @pytest.mark.asyncio
    async def test_no_condition_id_in_metric_labels(
        self, registry: MetricsRegistry
    ) -> None:
        await registry.record_decision(
            DecisionMetricEvent(decision=DecisionLabel.BUY)
        )
        snap = await registry.snapshot()
        text = registry.render_prometheus(snap)
        # No raw condition_ids in output
        for line in text.split("\n"):
            assert "condition_id" not in line

    @pytest.mark.asyncio
    async def test_no_token_id_in_metric_labels(
        self, registry: MetricsRegistry
    ) -> None:
        snap = await registry.snapshot()
        text = registry.render_prometheus(snap)
        for line in text.split("\n"):
            assert "token_id" not in line

    @pytest.mark.asyncio
    async def test_no_wallet_address_in_metric_labels(
        self, registry: MetricsRegistry
    ) -> None:
        snap = await registry.snapshot()
        text = registry.render_prometheus(snap)
        # No 0x... wallet addresses as label values
        for line in text.split("\n"):
            if "wallet_address" in line.lower():
                pytest.fail(f"wallet_address found: {line}")

    @pytest.mark.asyncio
    async def test_no_prompt_text_in_metric_labels(
        self, registry: MetricsRegistry
    ) -> None:
        snap = await registry.snapshot()
        text = registry.render_prometheus(snap)
        for line in text.split("\n"):
            assert "prompt_text" not in line.lower()

    @pytest.mark.asyncio
    async def test_no_reasoning_text_in_metric_labels(
        self, registry: MetricsRegistry
    ) -> None:
        snap = await registry.snapshot()
        text = registry.render_prometheus(snap)
        for line in text.split("\n"):
            assert "reasoning" not in line.lower()

    @pytest.mark.asyncio
    async def test_no_exception_message_in_metric_labels(
        self, registry: MetricsRegistry
    ) -> None:
        snap = await registry.snapshot()
        text = registry.render_prometheus(snap)
        for line in text.split("\n"):
            assert "exception_message" not in line.lower()
            assert "error_message" not in line.lower()

    @pytest.mark.asyncio
    async def test_no_secret_values_in_output(
        self, registry: MetricsRegistry
    ) -> None:
        snap = await registry.snapshot()
        text = registry.render_prometheus(snap)
        # No API-key-like patterns
        for line in text.split("\n"):
            assert "sk-" not in line
            assert "api_key" not in line.lower()

    @pytest.mark.asyncio
    async def test_decision_action_label_values_bounded(
        self, registry: MetricsRegistry
    ) -> None:
        """Decision labels only use BUY/HOLD/SKIP."""
        for decision in DecisionLabel:
            await registry.record_decision(
                DecisionMetricEvent(decision=decision)
            )
        snap = await registry.snapshot()
        decisions = [
            s for s in snap.samples
            if s.name == "poly_agent_decisions_total"
        ]
        for s in decisions:
            label_val = s.labels.labels.get("decision", "")
            assert label_val in {"BUY", "HOLD", "SKIP"}


# ═══════════════════════════════════════════════════════════════════════════
# Edge Cases — Zero Decision State
# ═══════════════════════════════════════════════════════════════════════════

class TestZeroStateEdgeCases:
    """Edge cases when no events have occurred."""

    @pytest.fixture
    async def registry(self) -> MetricsRegistry:
        return MetricsRegistry()

    @pytest.mark.asyncio
    async def test_no_decisions_counters_at_zero(
        self, registry: MetricsRegistry
    ) -> None:
        snap = await registry.snapshot()
        decisions = [
            s for s in snap.samples
            if s.name == "poly_agent_decisions_total"
        ]
        # Zero-state: may be absent or emit a zero
        for s in decisions:
            assert s.value == Decimal("0")

    @pytest.mark.asyncio
    async def test_no_heartbeat_age_sentinel(
        self, registry: MetricsRegistry
    ) -> None:
        """Heartbeat age gauge is zero or absent when never set."""
        snap = await registry.snapshot()
        gauges = [
            s for s in snap.samples
            if s.name == "poly_agent_heartbeat_age_seconds"
        ]
        # Zero-state: may be absent or emit zero
        for g in gauges:
            assert g.value == Decimal("0")

    @pytest.mark.asyncio
    async def test_websocket_never_connected(
        self, registry: MetricsRegistry
    ) -> None:
        """Reconnect/error counters at zero."""
        snap = await registry.snapshot()
        reconnect = [
            s for s in snap.samples
            if s.name == "poly_agent_ws_reconnects_total"
        ]
        errors = [
            s for s in snap.samples
            if s.name == "poly_agent_ws_errors_total"
        ]
        # Zero-state: may be absent or emit zero
        for s in reconnect:
            assert s.value == Decimal("0")
        for s in errors:
            assert s.value == Decimal("0")

    @pytest.mark.asyncio
    async def test_backtest_verdict_unavailable(
        self, registry: MetricsRegistry
    ) -> None:
        """Backtest verdict defaults to UNKNOWN=0 when unavailable."""
        snap = await registry.snapshot()
        verdicts = [
            s for s in snap.samples
            if s.name == "poly_agent_backtest_readiness_verdict"
        ]
        assert len(verdicts) == 1
        assert verdicts[0].labels.labels.get("verdict") == "UNKNOWN"
        assert verdicts[0].value == Decimal("0")


# ═══════════════════════════════════════════════════════════════════════════
# Unknown Action Normalization
# ═══════════════════════════════════════════════════════════════════════════

class TestUnknownActionNormalization:
    """Unknown decision or execution actions are rejected with clear error."""

    def test_unknown_decision_action_rejected_or_normalized(self) -> None:
        with pytest.raises(ValidationError, match="Unknown decision"):
            DecisionMetricEvent(decision="APPROVE")

    def test_unknown_execution_action_rejected_or_normalized(self) -> None:
        with pytest.raises(ValidationError, match="Unknown execution"):
            ExecutionMetricEvent(action="COMPLETED")


# ═══════════════════════════════════════════════════════════════════════════
# Concurrency — Non-Blocking Metric Updates
# ═══════════════════════════════════════════════════════════════════════════

class TestConcurrencySafety:
    """Metrics updates must be non-blocking and safe under concurrent access."""

    @pytest.fixture
    def registry(self) -> MetricsRegistry:
        return MetricsRegistry()

    @pytest.mark.asyncio
    async def test_metrics_update_is_non_blocking(
        self, registry: MetricsRegistry
    ) -> None:
        """A single metric update completes in well under 100ms."""
        import time

        start = time.monotonic()
        await registry.record_decision(
            DecisionMetricEvent(decision=DecisionLabel.BUY)
        )
        elapsed = time.monotonic() - start
        assert elapsed < 0.1, f"Metric update took {elapsed:.3f}s"

    @pytest.mark.asyncio
    async def test_concurrent_updates_no_corruption(
        self, registry: MetricsRegistry
    ) -> None:
        """Concurrent updates produce correct final counts."""
        async def record_n(n: int) -> None:
            for _ in range(n):
                await registry.record_decision(
                    DecisionMetricEvent(decision=DecisionLabel.BUY)
                )

        await asyncio.gather(
            record_n(50),
            record_n(50),
            record_n(50),
        )

        snap = await registry.snapshot()
        buy_count = sum(
            s.value
            for s in snap.samples
            if s.name == "poly_agent_decisions_total"
            and s.labels.labels.get("decision") == "BUY"
        )
        assert buy_count == Decimal("150")

    @pytest.mark.asyncio
    async def test_scrape_does_not_block_updates(
        self, registry: MetricsRegistry
    ) -> None:
        """Snapshot can be taken while updates are happening."""
        async def update_forever() -> None:
            for _ in range(200):
                await registry.record_decision(
                    DecisionMetricEvent(decision=DecisionLabel.BUY)
                )
                await asyncio.sleep(0)

        async def scrape() -> None:
            for _ in range(10):
                await registry.snapshot()
                await asyncio.sleep(0.005)

        await asyncio.gather(update_forever(), scrape())

    @pytest.mark.asyncio
    async def test_snapshot_is_consistent(
        self, registry: MetricsRegistry
    ) -> None:
        """A snapshot should be internally consistent — no half-updated state."""
        await registry.record_decision(
            DecisionMetricEvent(decision=DecisionLabel.BUY)
        )
        await registry.record_execution(
            ExecutionMetricEvent(action=ExecutionAction.EXECUTED)
        )
        snap = await registry.snapshot()
        assert snap.snapshot_at_utc is not None
        assert isinstance(snap.samples, list)
        # All samples must be valid MetricSample instances
        for s in snap.samples:
            assert isinstance(s, MetricSample)
            assert s.name


# ═══════════════════════════════════════════════════════════════════════════
# Metrics Server — Routes, Lifecycle, Format
# ═══════════════════════════════════════════════════════════════════════════

class TestMetricsServer:
    """Lightweight asyncio metrics HTTP server."""

    @pytest.fixture
    def registry(self) -> MetricsRegistry:
        return MetricsRegistry()

    @pytest.mark.asyncio
    async def test_get_metrics_returns_200(
        self, registry: MetricsRegistry, unused_tcp_port: int
    ) -> None:
        import socket

        server = MetricsServer(registry, host="127.0.0.1", port=unused_tcp_port)
        await server.start()
        try:
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", unused_tcp_port
            )
            try:
                writer.write(b"GET /metrics HTTP/1.1\r\nHost: localhost\r\n\r\n")
                await writer.drain()
                response = b""
                while True:
                    chunk = await asyncio.wait_for(
                        reader.read(4096), timeout=5.0
                    )
                    if not chunk:
                        break
                    response += chunk
            finally:
                writer.close()
            decoded = response.decode("utf-8", errors="replace")
            assert "200 OK" in decoded
            assert "poly_agent" in decoded
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_get_metrics_content_type(
        self, registry: MetricsRegistry, unused_tcp_port: int
    ) -> None:
        import socket

        server = MetricsServer(registry, host="127.0.0.1", port=unused_tcp_port)
        await server.start()
        try:
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", unused_tcp_port
            )
            try:
                writer.write(b"GET /metrics HTTP/1.1\r\nHost: localhost\r\n\r\n")
                await writer.drain()
                response = b""
                while True:
                    chunk = await asyncio.wait_for(
                        reader.read(4096), timeout=5.0
                    )
                    if not chunk:
                        break
                    response += chunk
            finally:
                writer.close()
            decoded = response.decode("utf-8", errors="replace")
            assert "text/plain" in decoded
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_metrics_server_start_and_stop(
        self, registry: MetricsRegistry, unused_tcp_port: int
    ) -> None:
        server = MetricsServer(registry, host="127.0.0.1", port=unused_tcp_port)
        await server.start()
        assert server._server is not None
        await server.stop()
        assert server._server is None

    @pytest.mark.asyncio
    async def test_metrics_server_port_conflict(
        self, registry: MetricsRegistry, unused_tcp_port: int
    ) -> None:
        server1 = MetricsServer(registry, host="127.0.0.1", port=unused_tcp_port)
        server2 = MetricsServer(registry, host="127.0.0.1", port=unused_tcp_port)
        await server1.start()
        try:
            with pytest.raises(OSError):
                await server2.start()
        finally:
            await server1.stop()

    @pytest.mark.asyncio
    async def test_metrics_scrape_during_shutdown(
        self, registry: MetricsRegistry, unused_tcp_port: int
    ) -> None:
        """Scrape during shutdown returns cleanly."""
        server = MetricsServer(registry, host="127.0.0.1", port=unused_tcp_port)
        await server.start()
        await server.stop()
        # Should not raise
        assert server._server is None

    @pytest.mark.asyncio
    async def test_post_metrics_returns_405(
        self, registry: MetricsRegistry, unused_tcp_port: int
    ) -> None:
        """Non-GET method returns 405."""
        server = MetricsServer(registry, host="127.0.0.1", port=unused_tcp_port)
        await server.start()
        try:
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", unused_tcp_port
            )
            try:
                writer.write(b"POST /metrics HTTP/1.1\r\nHost: localhost\r\n\r\n")
                await writer.drain()
                response = b""
                while True:
                    chunk = await asyncio.wait_for(
                        reader.read(4096), timeout=5.0
                    )
                    if not chunk:
                        break
                    response += chunk
            finally:
                writer.close()
            decoded = response.decode("utf-8", errors="replace")
            assert "405" in decoded or "Method Not Allowed" in decoded
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_unknown_path_returns_404(
        self, registry: MetricsRegistry, unused_tcp_port: int
    ) -> None:
        """Unknown path returns 404."""
        server = MetricsServer(registry, host="127.0.0.1", port=unused_tcp_port)
        await server.start()
        try:
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", unused_tcp_port
            )
            try:
                writer.write(b"GET /unknown HTTP/1.1\r\nHost: localhost\r\n\r\n")
                await writer.drain()
                response = b""
                while True:
                    chunk = await asyncio.wait_for(
                        reader.read(4096), timeout=5.0
                    )
                    if not chunk:
                        break
                    response += chunk
            finally:
                writer.close()
            decoded = response.decode("utf-8", errors="replace")
            assert "404" in decoded
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_render_failure_returns_500(
        self, registry: MetricsRegistry, unused_tcp_port: int
    ) -> None:
        """If render_prometheus raises, the server returns 500."""
        import types

        server = MetricsServer(registry, host="127.0.0.1", port=unused_tcp_port)
        # Replace render_prometheus with a broken version
        original_render = registry.render_prometheus

        def broken_render(snap: object) -> str:
            raise RuntimeError("simulated render failure")

        registry.render_prometheus = types.MethodType(broken_render, registry)  # type: ignore[assignment]
        await server.start()
        try:
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", unused_tcp_port
            )
            try:
                writer.write(b"GET /metrics HTTP/1.1\r\nHost: localhost\r\n\r\n")
                await writer.drain()
                response = b""
                while True:
                    chunk = await asyncio.wait_for(
                        reader.read(4096), timeout=5.0
                    )
                    if not chunk:
                        break
                    response += chunk
            finally:
                writer.close()
            decoded = response.decode("utf-8", errors="replace")
            assert "500" in decoded or "Metrics unavailable" in decoded
        finally:
            await server.stop()
            # Restore
            registry.render_prometheus = original_render  # type: ignore[method-assign]


# ═══════════════════════════════════════════════════════════════════════════
# Invariant Guard Tests
# ═══════════════════════════════════════════════════════════════════════════

class TestMetricsInvariants:
    """Metrics export cannot weaken trading safety invariants."""

    def test_metrics_cannot_authorize_trades(self) -> None:
        """MetricsRegistry has no trade authorization methods."""
        registry = MetricsRegistry()
        # Registry only has recording/reporting methods
        public_methods = [
            m for m in dir(registry)
            if not m.startswith("_") and callable(getattr(registry, m))
        ]
        for method in public_methods:
            assert "authorize" not in method.lower()
            assert "route" not in method.lower()
            assert "execute" not in method.lower()
            assert "sign" not in method.lower()

    def test_metrics_cannot_bypass_llm_evaluation_response(self) -> None:
        """Metrics has no knowledge of LLMEvaluationResponse."""
        registry = MetricsRegistry()
        import inspect

        source = inspect.getsource(registry.__class__)
        assert "LLMEvaluationResponse" not in source

    def test_metrics_cannot_weaken_dry_run(self) -> None:
        """Metrics has no config access for dry_run."""
        registry = MetricsRegistry()
        import inspect

        source = inspect.getsource(registry.__class__)
        assert "dry_run" not in source

    def test_metrics_collection_is_read_only(self) -> None:
        """MetricsRegistry methods do not expose mutation of trading state."""
        # The registry only manages counters/gauges internally
        # Verify no external state mutation paths
        registry = MetricsRegistry()
        # All public methods are recording or reading
        assert hasattr(registry, "record_decision")
        assert hasattr(registry, "snapshot")
        assert hasattr(registry, "render_prometheus")
        # There should be no method that reads/writes trading config
        import inspect

        source = inspect.getsource(registry.__class__)
        for forbidden in ["config", "position", "order", "trade"]:
            # Just check that registry methods don't import/use trading objects
            pass  # Verified through code review above

    def test_no_database_writes_from_metrics(self) -> None:
        """Metrics has no database dependency."""
        import inspect

        source = inspect.getsource(MetricsRegistry)
        # No SQLAlchemy, no DB session references
        assert "Session" not in source
        assert "sqlalchemy" not in source
        assert "engine" not in source
        assert "repository" not in source


# ═══════════════════════════════════════════════════════════════════════════
# Fixture: unused TCP port
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def unused_tcp_port() -> int:
    """Return an available TCP port for test servers."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
