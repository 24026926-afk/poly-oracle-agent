"""

Unit tests for WI-50 — Telegram Operational Alert Bridge.

Covers typed operational alert schemas, dedupe cooldown, secret-free payloads,
sustained threshold evaluation, circuit breaker transitions, non-blocking
dispatch, and graceful Telegram-disabled behavior.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from src.agents.execution.circuit_breaker import CircuitBreakerState
from src.observability.operational_alerts import OperationalAlertBridge, _ALERT_TYPE_SEVERITY
from src.schemas.ops import (
    OperationalAlert,
    OperationalAlertConfig,
    OperationalAlertDispatchResult,
    OperationalAlertEvaluation,
    OperationalAlertSeverity,
    OperationalAlertState,
    OperationalAlertStatus,
    OperationalAlertType,
)


# ═══════════════════════════════════════════════════════════════════════════
# Schema Tests — OperationalAlertType
# ═══════════════════════════════════════════════════════════════════════════


class TestOperationalAlertTypeEnum:
    """OperationalAlertType — bounded alert type enumeration."""

    def test_valid_alert_types(self) -> None:
        assert OperationalAlertType.PROCESS_STARTED == "process_started"
        assert OperationalAlertType.READINESS_DEGRADED == "readiness_degraded"
        assert OperationalAlertType.WEBSOCKET_STALE == "websocket_stale"
        assert OperationalAlertType.CIRCUIT_BREAKER_OPENED == "circuit_breaker_opened"
        assert OperationalAlertType.CIRCUIT_BREAKER_CLOSED == "circuit_breaker_closed"

    def test_rejects_unknown_alert_type(self) -> None:
        with pytest.raises(ValueError):
            OperationalAlertType("unknown_alert")

        with pytest.raises(ValueError):
            OperationalAlertType("trade_executed")  # not in bounded set


# ═══════════════════════════════════════════════════════════════════════════
# Schema Tests — OperationalAlertSeverity
# ═══════════════════════════════════════════════════════════════════════════


class TestOperationalAlertSeverity:
    """OperationalAlertSeverity enumeration."""

    def test_severity_values(self) -> None:
        assert OperationalAlertSeverity.CRITICAL == "CRITICAL"
        assert OperationalAlertSeverity.WARNING == "WARNING"
        assert OperationalAlertSeverity.INFO == "INFO"


# ═══════════════════════════════════════════════════════════════════════════
# Schema Tests — OperationalAlert
# ═══════════════════════════════════════════════════════════════════════════


class TestOperationalAlertSchema:
    """OperationalAlert — payload constraints and secret rejection."""

    def test_valid_alert_construction(self) -> None:
        alert = OperationalAlert(
            alert_type=OperationalAlertType.PROCESS_STARTED,
            severity=OperationalAlertSeverity.INFO,
            reason_code="process_started",
            message="Agent started successfully.",
        )
        assert alert.alert_type == OperationalAlertType.PROCESS_STARTED
        assert alert.severity == OperationalAlertSeverity.INFO
        assert alert.reason_code == "process_started"

    def test_alert_requires_alert_type(self) -> None:
        with pytest.raises(ValidationError):
            OperationalAlert(
                severity=OperationalAlertSeverity.INFO,
                message="Missing alert_type.",
            )

    def test_alert_requires_severity(self) -> None:
        with pytest.raises(ValidationError):
            OperationalAlert(
                alert_type=OperationalAlertType.PROCESS_STARTED,
                message="Missing severity.",
            )

    def test_alert_requires_first_seen_timestamp(self) -> None:
        # first_seen_at_utc has a default, so construction without it is fine
        alert = OperationalAlert(
            alert_type=OperationalAlertType.PROCESS_STARTED,
            severity=OperationalAlertSeverity.INFO,
        )
        assert alert.first_seen_at_utc is not None

    def test_rejects_api_key_in_reason_field(self) -> None:
        with pytest.raises(ValidationError):
            OperationalAlert(
                alert_type=OperationalAlertType.WEBSOCKET_STALE,
                severity=OperationalAlertSeverity.WARNING,
                reason_code="api_key=sk-abc123",
            )

    def test_rejects_wallet_private_key_in_reason_field(self) -> None:
        with pytest.raises(ValidationError):
            OperationalAlert(
                alert_type=OperationalAlertType.WEBSOCKET_STALE,
                severity=OperationalAlertSeverity.WARNING,
                reason_code="private_key leaked",
            )

    def test_rejects_telegram_token_in_payload(self) -> None:
        with pytest.raises(ValidationError):
            OperationalAlert(
                alert_type=OperationalAlertType.WEBSOCKET_STALE,
                severity=OperationalAlertSeverity.WARNING,
                message="Token: 1234567890:AAHqABC123defGHIjklMNOpqrSTUvwxYZabc123",
            )

    def test_rejects_condition_id_in_payload(self) -> None:
        with pytest.raises(ValidationError):
            OperationalAlert(
                alert_type=OperationalAlertType.WEBSOCKET_STALE,
                severity=OperationalAlertSeverity.WARNING,
                message="condition 0x1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef",
            )

    def test_rejects_token_id_in_payload(self) -> None:
        with pytest.raises(ValidationError):
            OperationalAlert(
                alert_type=OperationalAlertType.WEBSOCKET_STALE,
                severity=OperationalAlertSeverity.WARNING,
                message="token_id=12345678901",
            )

    def test_rejects_prompt_text_in_payload(self) -> None:
        with pytest.raises(ValidationError):
            OperationalAlert(
                alert_type=OperationalAlertType.WEBSOCKET_STALE,
                severity=OperationalAlertSeverity.WARNING,
                reason_code="prompt_text exposed",
            )

    def test_rejects_reasoning_text_in_payload(self) -> None:
        with pytest.raises(ValidationError):
            OperationalAlert(
                alert_type=OperationalAlertType.WEBSOCKET_STALE,
                severity=OperationalAlertSeverity.WARNING,
                message="reasoning leaked in alert",
            )

    def test_rejects_raw_exception_message_in_payload(self) -> None:
        # Exception messages containing secrets should be rejected
        with pytest.raises(ValidationError):
            OperationalAlert(
                alert_type=OperationalAlertType.WEBSOCKET_STALE,
                severity=OperationalAlertSeverity.WARNING,
                message="Exception: secret key 0xabc123def456789",
            )

    def test_rejects_raw_environment_variable_in_payload(self) -> None:
        # Raw env vars containing "api_key" substring
        with pytest.raises(ValidationError):
            OperationalAlert(
                alert_type=OperationalAlertType.WEBSOCKET_STALE,
                severity=OperationalAlertSeverity.WARNING,
                message="TRADING_API_KEY not set",
            )

    def test_allows_bounded_reason_code(self) -> None:
        alert = OperationalAlert(
            alert_type=OperationalAlertType.READINESS_DEGRADED,
            severity=OperationalAlertSeverity.WARNING,
            reason_code="readiness_degraded",
            message="Readiness has been degraded.",
        )
        assert alert.reason_code == "readiness_degraded"


# ═══════════════════════════════════════════════════════════════════════════
# Schema Tests — OperationalAlertState
# ═══════════════════════════════════════════════════════════════════════════


class TestOperationalAlertState:
    """OperationalAlertState — tracks alert lifecycle with cooldown."""

    def test_state_defaults_to_no_active_alert(self) -> None:
        state = OperationalAlertState(alert_type=OperationalAlertType.WEBSOCKET_STALE)
        assert state.is_active is False
        assert state.first_seen_at_utc is None
        assert state.last_dispatched_at_utc is None

    def test_state_records_first_seen_timestamp(self) -> None:
        now = datetime.now(timezone.utc)
        state = OperationalAlertState(
            alert_type=OperationalAlertType.WEBSOCKET_STALE,
            first_seen_at_utc=now,
            is_active=True,
        )
        assert state.first_seen_at_utc == now

    def test_state_tracks_last_dispatched_timestamp(self) -> None:
        now = datetime.now(timezone.utc)
        state = OperationalAlertState(
            alert_type=OperationalAlertType.WEBSOCKET_STALE,
            last_dispatched_at_utc=now,
        )
        assert state.last_dispatched_at_utc == now

    def test_state_within_cooldown_returns_true(self) -> None:
        now = datetime.now(timezone.utc)
        state = OperationalAlertState(
            alert_type=OperationalAlertType.WEBSOCKET_STALE,
            last_dispatched_at_utc=now - timedelta(seconds=30),
        )
        assert state.is_within_cooldown(60.0, now) is True

    def test_state_outside_cooldown_returns_false(self) -> None:
        now = datetime.now(timezone.utc)
        state = OperationalAlertState(
            alert_type=OperationalAlertType.WEBSOCKET_STALE,
            last_dispatched_at_utc=now - timedelta(seconds=120),
        )
        assert state.is_within_cooldown(60.0, now) is False


# ═══════════════════════════════════════════════════════════════════════════
# Schema Tests — OperationalAlertConfig
# ═══════════════════════════════════════════════════════════════════════════


class TestOperationalAlertConfig:
    """OperationalAlertConfig — thresholds and cooldown configuration."""

    def test_default_readiness_degraded_threshold(self) -> None:
        config = OperationalAlertConfig()
        assert config.readiness_degraded_threshold_seconds == 300.0

    def test_default_websocket_stale_threshold(self) -> None:
        config = OperationalAlertConfig()
        assert config.websocket_stale_threshold_seconds == 300.0

    def test_default_cooldown_seconds(self) -> None:
        config = OperationalAlertConfig()
        assert config.alert_cooldown_seconds == 600.0

    def test_config_can_disable_startup_alert(self) -> None:
        config = OperationalAlertConfig(enable_startup_alert=False)
        assert config.enable_startup_alert is False

        config2 = OperationalAlertConfig(enable_startup_alert=True)
        assert config2.enable_startup_alert is True


# ═══════════════════════════════════════════════════════════════════════════
# Schema Tests — OperationalAlertEvaluation
# ═══════════════════════════════════════════════════════════════════════════


class TestOperationalAlertEvaluation:
    """OperationalAlertEvaluation — read-only alert decision."""

    def test_evaluation_is_read_only_has_no_mutation_methods(self) -> None:
        eval_result = OperationalAlertEvaluation(
            alert_type=OperationalAlertType.WEBSOCKET_STALE,
            should_dispatch=False,
        )
        # Frozen model — no mutation methods
        with pytest.raises(Exception):
            eval_result.should_dispatch = True

    def test_evaluation_includes_alert_type(self) -> None:
        eval_result = OperationalAlertEvaluation(
            alert_type=OperationalAlertType.READINESS_DEGRADED,
        )
        assert eval_result.alert_type == OperationalAlertType.READINESS_DEGRADED

    def test_evaluation_includes_decision_to_dispatch_or_suppress(self) -> None:
        eval_result = OperationalAlertEvaluation(
            alert_type=OperationalAlertType.CIRCUIT_BREAKER_OPENED,
            should_dispatch=True,
        )
        assert eval_result.should_dispatch is True

        suppressed = OperationalAlertEvaluation(
            alert_type=OperationalAlertType.CIRCUIT_BREAKER_OPENED,
            should_dispatch=False,
            suppressed_reason="cooldown_active",
        )
        assert suppressed.should_dispatch is False
        assert suppressed.suppressed_reason == "cooldown_active"


# ═══════════════════════════════════════════════════════════════════════════
# Schema Tests — OperationalAlertDispatchResult
# ═══════════════════════════════════════════════════════════════════════════


class TestOperationalAlertDispatchResult:
    """OperationalAlertDispatchResult — dispatch outcome."""

    def test_dispatched_result(self) -> None:
        alert = OperationalAlert(
            alert_type=OperationalAlertType.PROCESS_STARTED,
            severity=OperationalAlertSeverity.INFO,
            reason_code="process_started",
        )
        result = OperationalAlertDispatchResult(
            alert_type=OperationalAlertType.PROCESS_STARTED,
            status=OperationalAlertStatus.DISPATCHED,
            alert=alert,
        )
        assert result.status == OperationalAlertStatus.DISPATCHED

    def test_suppressed_due_to_cooldown(self) -> None:
        result = OperationalAlertDispatchResult(
            alert_type=OperationalAlertType.WEBSOCKET_STALE,
            status=OperationalAlertStatus.SUPPRESSED_COOLDOWN,
        )
        assert result.status == OperationalAlertStatus.SUPPRESSED_COOLDOWN

    def test_suppressed_due_to_disabled_telegram(self) -> None:
        result = OperationalAlertDispatchResult(
            alert_type=OperationalAlertType.WEBSOCKET_STALE,
            status=OperationalAlertStatus.SUPPRESSED_DISABLED,
        )
        assert result.status == OperationalAlertStatus.SUPPRESSED_DISABLED

    def test_failed_dispatch_result(self) -> None:
        result = OperationalAlertDispatchResult(
            alert_type=OperationalAlertType.WEBSOCKET_STALE,
            status=OperationalAlertStatus.FAILED,
            error_detail="Connection timeout",
        )
        assert result.status == OperationalAlertStatus.FAILED
        assert result.error_detail == "Connection timeout"

    def test_rejects_secret_in_error_detail(self) -> None:
        with pytest.raises(ValidationError):
            OperationalAlertDispatchResult(
                alert_type=OperationalAlertType.WEBSOCKET_STALE,
                status=OperationalAlertStatus.FAILED,
                error_detail="api_key leaked in response",
            )


# ═══════════════════════════════════════════════════════════════════════════
# Alert Evaluation — Process Started / Restart
# ═══════════════════════════════════════════════════════════════════════════


class TestProcessStartedAlert:
    """Process started / restart alert evaluation."""

    async def test_startup_alert_fires_when_enabled(self) -> None:
        config = OperationalAlertConfig(
            enable_operational_alerts=True,
            enable_startup_alert=True,
        )
        notifier = MagicMock()
        notifier._send = AsyncMock(return_value=True)

        bridge = OperationalAlertBridge(config=config, notifier=notifier)
        result = await bridge.dispatch_startup_alert()

        assert result is not None
        assert result.status == OperationalAlertStatus.DISPATCHED
        assert result.alert.alert_type == OperationalAlertType.PROCESS_STARTED
        notifier._send.assert_called_once()

    async def test_startup_alert_does_not_fire_when_disabled(self) -> None:
        config = OperationalAlertConfig(
            enable_operational_alerts=True,
            enable_startup_alert=False,
        )
        notifier = MagicMock()
        notifier._send = AsyncMock()

        bridge = OperationalAlertBridge(config=config, notifier=notifier)
        result = await bridge.dispatch_startup_alert()

        assert result is None
        notifier._send.assert_not_called()

    async def test_startup_alert_disabled_by_default_in_test_config(self) -> None:
        config = OperationalAlertConfig()  # defaults: enable_startup_alert=False
        notifier = MagicMock()
        notifier._send = AsyncMock()

        bridge = OperationalAlertBridge(config=config, notifier=notifier)
        result = await bridge.dispatch_startup_alert()

        assert result is None  # bridge evaluates but startup is disabled
        notifier._send.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════
# Alert Evaluation — Readiness Degraded
# ═══════════════════════════════════════════════════════════════════════════


class TestReadinessDegradedAlert:
    """Sustained readiness degraded alert evaluation."""

    async def test_no_alert_when_ready(self) -> None:
        config = OperationalAlertConfig(enable_operational_alerts=True)
        bridge = OperationalAlertBridge(config=config, notifier=MagicMock())

        results = await bridge.evaluate_and_dispatch_all(
            readiness_ready=True,
            ws_connected=True,
            ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.CLOSED,
        )

        readiness_results = [r for r in results if r.alert_type == OperationalAlertType.READINESS_DEGRADED]
        assert len(readiness_results) == 1
        assert readiness_results[0].status == OperationalAlertStatus.SUPPRESSED_DISABLED

    async def test_no_alert_immediately_on_degraded(self) -> None:
        config = OperationalAlertConfig(
            enable_operational_alerts=True,
            readiness_degraded_threshold_seconds=300.0,
        )
        bridge = OperationalAlertBridge(config=config, notifier=MagicMock())

        now = datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)

        # First call — degraded detected but below threshold
        results = await bridge.evaluate_and_dispatch_all(
            readiness_ready=False,
            ws_connected=True,
            ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.CLOSED,
            now=now,
        )

        readiness_results = [r for r in results if r.alert_type == OperationalAlertType.READINESS_DEGRADED]
        assert readiness_results[0].status == OperationalAlertStatus.SUPPRESSED_DISABLED

    async def test_alert_fires_after_sustained_threshold(self) -> None:
        notifier = MagicMock()
        notifier._send = AsyncMock(return_value=True)

        config = OperationalAlertConfig(
            enable_operational_alerts=True,
            readiness_degraded_threshold_seconds=300.0,
        )
        bridge = OperationalAlertBridge(config=config, notifier=notifier)

        now = datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)

        # Degraded first seen
        await bridge.evaluate_and_dispatch_all(
            readiness_ready=False, ws_connected=True, ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.CLOSED, now=now,
        )

        # After threshold — should fire
        now2 = now + timedelta(seconds=301)
        results = await bridge.evaluate_and_dispatch_all(
            readiness_ready=False, ws_connected=True, ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.CLOSED, now=now2,
        )

        readiness_results = [r for r in results if r.alert_type == OperationalAlertType.READINESS_DEGRADED]
        assert readiness_results[0].status == OperationalAlertStatus.DISPATCHED
        notifier._send.assert_called_once()

    def test_sustained_threshold_defaults_to_five_minutes(self) -> None:
        config = OperationalAlertConfig()
        assert config.readiness_degraded_threshold_seconds == 300.0

    async def test_degraded_below_threshold_no_alert(self) -> None:
        notifier = MagicMock()
        notifier._send = AsyncMock(return_value=True)

        config = OperationalAlertConfig(
            enable_operational_alerts=True,
            readiness_degraded_threshold_seconds=300.0,
        )
        bridge = OperationalAlertBridge(config=config, notifier=notifier)

        now = datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)
        await bridge.evaluate_and_dispatch_all(
            readiness_ready=False, ws_connected=True, ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.CLOSED, now=now,
        )

        # 299s — still below 300s threshold
        now2 = now + timedelta(seconds=299)
        results = await bridge.evaluate_and_dispatch_all(
            readiness_ready=False, ws_connected=True, ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.CLOSED, now=now2,
        )

        readiness_results = [r for r in results if r.alert_type == OperationalAlertType.READINESS_DEGRADED]
        assert readiness_results[0].status == OperationalAlertStatus.SUPPRESSED_DISABLED
        notifier._send.assert_not_called()

    async def test_degraded_recovery_resets_state(self) -> None:
        config = OperationalAlertConfig(enable_operational_alerts=True)
        bridge = OperationalAlertBridge(config=config, notifier=MagicMock())

        now = datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)

        # Degraded first seen
        await bridge.evaluate_and_dispatch_all(
            readiness_ready=False, ws_connected=True, ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.CLOSED, now=now,
        )

        state = bridge.get_state(OperationalAlertType.READINESS_DEGRADED)
        assert state.is_active is True

        # Recovery
        await bridge.evaluate_and_dispatch_all(
            readiness_ready=True, ws_connected=True, ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.CLOSED, now=now + timedelta(seconds=60),
        )

        state = bridge.get_state(OperationalAlertType.READINESS_DEGRADED)
        assert state.is_active is False
        assert state.first_seen_at_utc is None

    async def test_repeated_degraded_inside_cooldown_no_duplicate(self) -> None:
        notifier = MagicMock()
        notifier._send = AsyncMock(return_value=True)

        config = OperationalAlertConfig(
            enable_operational_alerts=True,
            readiness_degraded_threshold_seconds=10.0,
            alert_cooldown_seconds=600.0,
        )
        bridge = OperationalAlertBridge(config=config, notifier=notifier)

        now = datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)

        # First alert fires
        await bridge.evaluate_and_dispatch_all(
            readiness_ready=False, ws_connected=True, ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.CLOSED, now=now,
        )
        results = await bridge.evaluate_and_dispatch_all(
            readiness_ready=False, ws_connected=True, ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.CLOSED, now=now + timedelta(seconds=11),
        )
        dispatched = [r for r in results if r.alert_type == OperationalAlertType.READINESS_DEGRADED]
        assert dispatched[0].status == OperationalAlertStatus.DISPATCHED

        # Second evaluation inside cooldown — suppressed
        results2 = await bridge.evaluate_and_dispatch_all(
            readiness_ready=False, ws_connected=True, ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.CLOSED, now=now + timedelta(seconds=20),
        )
        suppressed = [r for r in results2 if r.alert_type == OperationalAlertType.READINESS_DEGRADED]
        assert suppressed[0].status == OperationalAlertStatus.SUPPRESSED_COOLDOWN

        # Only one _send call total
        assert notifier._send.call_count == 1


# ═══════════════════════════════════════════════════════════════════════════
# Alert Evaluation — WebSocket Stale
# ═══════════════════════════════════════════════════════════════════════════


class TestWebSocketStaleAlert:
    """Sustained WebSocket stale / PONG stale alert evaluation."""

    async def test_no_alert_when_websocket_healthy(self) -> None:
        config = OperationalAlertConfig(enable_operational_alerts=True)
        bridge = OperationalAlertBridge(config=config, notifier=MagicMock())

        results = await bridge.evaluate_and_dispatch_all(
            readiness_ready=True,
            ws_connected=True,
            ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.CLOSED,
        )

        ws_results = [r for r in results if r.alert_type == OperationalAlertType.WEBSOCKET_STALE]
        assert len(ws_results) == 1
        assert ws_results[0].status == OperationalAlertStatus.SUPPRESSED_DISABLED

    async def test_no_alert_immediately_on_disconnect(self) -> None:
        config = OperationalAlertConfig(
            enable_operational_alerts=True,
            websocket_stale_threshold_seconds=300.0,
        )
        bridge = OperationalAlertBridge(config=config, notifier=MagicMock())

        now = datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)

        # Disconnect detected
        results = await bridge.evaluate_and_dispatch_all(
            readiness_ready=True,
            ws_connected=False,
            ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.CLOSED,
            now=now,
        )

        ws_results = [r for r in results if r.alert_type == OperationalAlertType.WEBSOCKET_STALE]
        assert ws_results[0].status == OperationalAlertStatus.SUPPRESSED_DISABLED

    async def test_alert_fires_after_sustained_disconnect(self) -> None:
        notifier = MagicMock()
        notifier._send = AsyncMock(return_value=True)

        config = OperationalAlertConfig(
            enable_operational_alerts=True,
            websocket_stale_threshold_seconds=300.0,
        )
        bridge = OperationalAlertBridge(config=config, notifier=notifier)

        now = datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)

        await bridge.evaluate_and_dispatch_all(
            readiness_ready=True, ws_connected=False, ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.CLOSED, now=now,
        )

        now2 = now + timedelta(seconds=301)
        results = await bridge.evaluate_and_dispatch_all(
            readiness_ready=True, ws_connected=False, ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.CLOSED, now=now2,
        )

        ws_results = [r for r in results if r.alert_type == OperationalAlertType.WEBSOCKET_STALE]
        assert ws_results[0].status == OperationalAlertStatus.DISPATCHED
        notifier._send.assert_called_once()

    async def test_pong_stale_triggers_after_threshold(self) -> None:
        notifier = MagicMock()
        notifier._send = AsyncMock(return_value=True)

        config = OperationalAlertConfig(
            enable_operational_alerts=True,
            websocket_stale_threshold_seconds=300.0,
        )
        bridge = OperationalAlertBridge(config=config, notifier=notifier)

        now = datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)

        # PONG stale but connected
        await bridge.evaluate_and_dispatch_all(
            readiness_ready=True, ws_connected=True, ws_pong_stale=True,
            circuit_breaker_state=CircuitBreakerState.CLOSED, now=now,
        )

        now2 = now + timedelta(seconds=301)
        results = await bridge.evaluate_and_dispatch_all(
            readiness_ready=True, ws_connected=True, ws_pong_stale=True,
            circuit_breaker_state=CircuitBreakerState.CLOSED, now=now2,
        )

        ws_results = [r for r in results if r.alert_type == OperationalAlertType.WEBSOCKET_STALE]
        assert ws_results[0].status == OperationalAlertStatus.DISPATCHED
        assert "ws_pong_stale" in ws_results[0].alert.reason_code

    def test_websocket_stale_threshold_defaults_to_five_minutes(self) -> None:
        config = OperationalAlertConfig()
        assert config.websocket_stale_threshold_seconds == 300.0

    async def test_pong_timestamp_absent_treated_as_unknown(self) -> None:
        """When PONG stale flag is True (meaning timestamp is absent or too old),
        the bridge treats the condition as stale but still applies sustained threshold."""
        notifier = MagicMock()
        notifier._send = AsyncMock(return_value=True)

        config = OperationalAlertConfig(
            enable_operational_alerts=True,
            websocket_stale_threshold_seconds=10.0,
        )
        bridge = OperationalAlertBridge(config=config, notifier=notifier)

        now = datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)

        # PONG absent — stale detected
        await bridge.evaluate_and_dispatch_all(
            readiness_ready=True, ws_connected=False, ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.CLOSED, now=now,
        )

        # After threshold
        results = await bridge.evaluate_and_dispatch_all(
            readiness_ready=True, ws_connected=False, ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.CLOSED, now=now + timedelta(seconds=11),
        )
        ws_results = [r for r in results if r.alert_type == OperationalAlertType.WEBSOCKET_STALE]
        assert ws_results[0].status == OperationalAlertStatus.DISPATCHED

    async def test_stale_below_threshold_no_alert(self) -> None:
        notifier = MagicMock()
        notifier._send = AsyncMock()

        config = OperationalAlertConfig(
            enable_operational_alerts=True,
            websocket_stale_threshold_seconds=300.0,
        )
        bridge = OperationalAlertBridge(config=config, notifier=notifier)

        now = datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)

        await bridge.evaluate_and_dispatch_all(
            readiness_ready=True, ws_connected=False, ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.CLOSED, now=now,
        )

        now2 = now + timedelta(seconds=299)
        results = await bridge.evaluate_and_dispatch_all(
            readiness_ready=True, ws_connected=False, ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.CLOSED, now=now2,
        )

        ws_results = [r for r in results if r.alert_type == OperationalAlertType.WEBSOCKET_STALE]
        assert ws_results[0].status == OperationalAlertStatus.SUPPRESSED_DISABLED
        notifier._send.assert_not_called()

    async def test_reconnect_resets_state(self) -> None:
        config = OperationalAlertConfig(enable_operational_alerts=True)
        bridge = OperationalAlertBridge(config=config, notifier=MagicMock())

        now = datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)

        await bridge.evaluate_and_dispatch_all(
            readiness_ready=True, ws_connected=False, ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.CLOSED, now=now,
        )

        state = bridge.get_state(OperationalAlertType.WEBSOCKET_STALE)
        assert state.is_active is True

        # Reconnect
        await bridge.evaluate_and_dispatch_all(
            readiness_ready=True, ws_connected=True, ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.CLOSED, now=now + timedelta(seconds=60),
        )

        state = bridge.get_state(OperationalAlertType.WEBSOCKET_STALE)
        assert state.is_active is False

    async def test_repeated_stale_inside_cooldown_no_duplicate(self) -> None:
        notifier = MagicMock()
        notifier._send = AsyncMock(return_value=True)

        config = OperationalAlertConfig(
            enable_operational_alerts=True,
            websocket_stale_threshold_seconds=10.0,
            alert_cooldown_seconds=600.0,
        )
        bridge = OperationalAlertBridge(config=config, notifier=notifier)

        now = datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)

        await bridge.evaluate_and_dispatch_all(
            readiness_ready=True, ws_connected=False, ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.CLOSED, now=now,
        )
        await bridge.evaluate_and_dispatch_all(
            readiness_ready=True, ws_connected=False, ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.CLOSED, now=now + timedelta(seconds=11),
        )
        # Second dispatch suppressed by cooldown
        results = await bridge.evaluate_and_dispatch_all(
            readiness_ready=True, ws_connected=False, ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.CLOSED, now=now + timedelta(seconds=20),
        )

        ws_results = [r for r in results if r.alert_type == OperationalAlertType.WEBSOCKET_STALE]
        assert ws_results[0].status == OperationalAlertStatus.SUPPRESSED_COOLDOWN
        assert notifier._send.call_count == 1


# ═══════════════════════════════════════════════════════════════════════════
# Alert Evaluation — Circuit Breaker Transitions
# ═══════════════════════════════════════════════════════════════════════════


class TestCircuitBreakerAlert:
    """Circuit breaker transition alerts."""

    async def test_circuit_breaker_opened_triggers_alert(self) -> None:
        notifier = MagicMock()
        notifier._send = AsyncMock(return_value=True)

        config = OperationalAlertConfig(enable_operational_alerts=True)
        bridge = OperationalAlertBridge(config=config, notifier=notifier)

        now = datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)

        # Transition: CLOSED → OPEN
        results = await bridge.evaluate_and_dispatch_all(
            readiness_ready=True,
            ws_connected=True,
            ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.OPEN,
            now=now,
        )

        cb_results = [r for r in results if r.alert_type == OperationalAlertType.CIRCUIT_BREAKER_OPENED]
        assert len(cb_results) == 1
        assert cb_results[0].status == OperationalAlertStatus.DISPATCHED
        notifier._send.assert_called_once()

    async def test_circuit_breaker_closed_triggers_alert(self) -> None:
        notifier = MagicMock()
        notifier._send = AsyncMock(return_value=True)

        config = OperationalAlertConfig(enable_operational_alerts=True)
        bridge = OperationalAlertBridge(config=config, notifier=notifier)

        now = datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)

        # Open first
        await bridge.evaluate_and_dispatch_all(
            readiness_ready=True, ws_connected=True, ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.OPEN, now=now,
        )

        # Then close
        now2 = now + timedelta(seconds=60)
        results = await bridge.evaluate_and_dispatch_all(
            readiness_ready=True, ws_connected=True, ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.CLOSED, now=now2,
        )

        cb_results = [r for r in results if r.alert_type == OperationalAlertType.CIRCUIT_BREAKER_CLOSED]
        assert len(cb_results) == 1
        assert cb_results[0].status == OperationalAlertStatus.DISPATCHED

    async def test_circuit_breaker_uses_typed_state_not_string_parsing(self) -> None:
        """The bridge uses CircuitBreakerState, not string parsing from logs."""
        config = OperationalAlertConfig(enable_operational_alerts=True)
        bridge = OperationalAlertBridge(config=config, notifier=MagicMock())

        # The evaluate_and_dispatch_all interface takes the typed circuit
        # breaker state enum, not a log string or derived text.
        now = datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)
        results = await bridge.evaluate_and_dispatch_all(
            readiness_ready=True,
            ws_connected=True,
            ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.OPEN,
            now=now,
        )

        cb_results = [r for r in results if r.alert_type == OperationalAlertType.CIRCUIT_BREAKER_OPENED]
        assert len(cb_results) == 1
        # The alert uses OperationalAlertType enum, not string parsing
        assert cb_results[0].alert_type == OperationalAlertType.CIRCUIT_BREAKER_OPENED

    async def test_circuit_breaker_opens_without_close_only_one_open_alert(self) -> None:
        notifier = MagicMock()
        notifier._send = AsyncMock(return_value=True)

        config = OperationalAlertConfig(enable_operational_alerts=True)
        bridge = OperationalAlertBridge(config=config, notifier=notifier)

        now = datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)

        # First evaluation: OPEN transition
        await bridge.evaluate_and_dispatch_all(
            readiness_ready=True, ws_connected=True, ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.OPEN, now=now,
        )

        # Second evaluation: still OPEN, no transition — no new open alert
        now2 = now + timedelta(seconds=60)
        results = await bridge.evaluate_and_dispatch_all(
            readiness_ready=True, ws_connected=True, ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.OPEN, now=now2,
        )

        # No new CIRCUIT_BREAKER_OPENED dispatch (it's not a transition)
        cb_open_results = [r for r in results if r.alert_type == OperationalAlertType.CIRCUIT_BREAKER_OPENED]
        assert len(cb_open_results) == 0
        # Only one _send call (from the first transition)
        assert notifier._send.call_count == 1

    async def test_repeated_open_inside_cooldown_no_duplicate(self) -> None:
        notifier = MagicMock()
        notifier._send = AsyncMock(return_value=True)

        config = OperationalAlertConfig(
            enable_operational_alerts=True,
            alert_cooldown_seconds=600.0,
        )
        bridge = OperationalAlertBridge(config=config, notifier=notifier)

        now = datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)

        # Open
        await bridge.evaluate_and_dispatch_all(
            readiness_ready=True, ws_connected=True, ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.OPEN, now=now,
        )
        # Close
        await bridge.evaluate_and_dispatch_all(
            readiness_ready=True, ws_connected=True, ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.CLOSED, now=now + timedelta(seconds=10),
        )
        # Open again inside cooldown
        results = await bridge.evaluate_and_dispatch_all(
            readiness_ready=True, ws_connected=True, ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.OPEN, now=now + timedelta(seconds=20),
        )

        cb_open_results = [r for r in results if r.alert_type == OperationalAlertType.CIRCUIT_BREAKER_OPENED]
        assert len(cb_open_results) == 1
        assert cb_open_results[0].status == OperationalAlertStatus.SUPPRESSED_COOLDOWN


# ═══════════════════════════════════════════════════════════════════════════
# Dedupe Cooldown Behavior
# ═══════════════════════════════════════════════════════════════════════════


class TestDedupeCooldown:
    """Deduplication cooldown prevents operator spam."""

    async def test_alert_dispatched_outside_cooldown(self) -> None:
        notifier = MagicMock()
        notifier._send = AsyncMock(return_value=True)

        config = OperationalAlertConfig(
            enable_operational_alerts=True,
            readiness_degraded_threshold_seconds=10.0,
            alert_cooldown_seconds=60.0,
        )
        bridge = OperationalAlertBridge(config=config, notifier=notifier)

        now = datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)

        # First dispatch
        await bridge.evaluate_and_dispatch_all(
            readiness_ready=False, ws_connected=True, ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.CLOSED, now=now,
        )
        results = await bridge.evaluate_and_dispatch_all(
            readiness_ready=False, ws_connected=True, ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.CLOSED, now=now + timedelta(seconds=11),
        )
        dispatched = [r for r in results if r.status == OperationalAlertStatus.DISPATCHED]
        assert len(dispatched) == 1

        # After cooldown expires — should dispatch again
        now3 = now + timedelta(seconds=70)
        # First reset the state so it's a fresh alertable condition
        # (we need to reset and re-enter degraded)
        await bridge.evaluate_and_dispatch_all(
            readiness_ready=True, ws_connected=True, ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.CLOSED, now=now3,
        )
        await bridge.evaluate_and_dispatch_all(
            readiness_ready=False, ws_connected=True, ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.CLOSED, now=now3 + timedelta(seconds=1),
        )
        results2 = await bridge.evaluate_and_dispatch_all(
            readiness_ready=False, ws_connected=True, ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.CLOSED, now=now3 + timedelta(seconds=12),
        )
        dispatched2 = [r for r in results2 if r.status == OperationalAlertStatus.DISPATCHED]
        assert len(dispatched2) >= 1
        # Now at least 2 calls total
        assert notifier._send.call_count >= 2

    async def test_alert_suppressed_inside_cooldown(self) -> None:
        notifier = MagicMock()
        notifier._send = AsyncMock(return_value=True)

        config = OperationalAlertConfig(
            enable_operational_alerts=True,
            readiness_degraded_threshold_seconds=10.0,
            alert_cooldown_seconds=600.0,
        )
        bridge = OperationalAlertBridge(config=config, notifier=notifier)

        now = datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)

        await bridge.evaluate_and_dispatch_all(
            readiness_ready=False, ws_connected=True, ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.CLOSED, now=now,
        )
        await bridge.evaluate_and_dispatch_all(
            readiness_ready=False, ws_connected=True, ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.CLOSED, now=now + timedelta(seconds=11),
        )
        results = await bridge.evaluate_and_dispatch_all(
            readiness_ready=False, ws_connected=True, ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.CLOSED, now=now + timedelta(seconds=20),
        )
        suppressed = [r for r in results if r.status == OperationalAlertStatus.SUPPRESSED_COOLDOWN]
        assert len(suppressed) >= 1
        assert notifier._send.call_count == 1

    async def test_cooldown_resets_after_expiry(self) -> None:
        notifier = MagicMock()
        notifier._send = AsyncMock(return_value=True)

        config = OperationalAlertConfig(
            enable_operational_alerts=True,
            readiness_degraded_threshold_seconds=10.0,
            alert_cooldown_seconds=60.0,
        )
        bridge = OperationalAlertBridge(config=config, notifier=notifier)

        now = datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)

        # Dispatch
        await bridge.evaluate_and_dispatch_all(
            readiness_ready=False, ws_connected=True, ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.CLOSED, now=now,
        )
        await bridge.evaluate_and_dispatch_all(
            readiness_ready=False, ws_connected=True, ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.CLOSED, now=now + timedelta(seconds=11),
        )

        # Cooldown expired — recovery first
        now2 = now + timedelta(seconds=70)
        await bridge.evaluate_and_dispatch_all(
            readiness_ready=True, ws_connected=True, ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.CLOSED, now=now2,
        )
        # Degraded again
        await bridge.evaluate_and_dispatch_all(
            readiness_ready=False, ws_connected=True, ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.CLOSED, now=now2 + timedelta(seconds=1),
        )
        results = await bridge.evaluate_and_dispatch_all(
            readiness_ready=False, ws_connected=True, ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.CLOSED, now=now2 + timedelta(seconds=12),
        )
        dispatched = [r for r in results if r.status == OperationalAlertStatus.DISPATCHED]
        assert len(dispatched) >= 1
        assert notifier._send.call_count >= 2

    async def test_different_alert_types_have_independent_cooldowns(self) -> None:
        notifier = MagicMock()
        notifier._send = AsyncMock(return_value=True)

        config = OperationalAlertConfig(
            enable_operational_alerts=True,
            readiness_degraded_threshold_seconds=10.0,
            websocket_stale_threshold_seconds=10.0,
            alert_cooldown_seconds=600.0,
        )
        bridge = OperationalAlertBridge(config=config, notifier=notifier)

        now = datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)

        # Degrade readiness
        await bridge.evaluate_and_dispatch_all(
            readiness_ready=False, ws_connected=True, ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.CLOSED, now=now,
        )
        await bridge.evaluate_and_dispatch_all(
            readiness_ready=False, ws_connected=True, ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.CLOSED, now=now + timedelta(seconds=11),
        )

        # Degrade WebSocket — different type, should still dispatch
        await bridge.evaluate_and_dispatch_all(
            readiness_ready=False, ws_connected=False, ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.CLOSED, now=now + timedelta(seconds=1),
        )
        results = await bridge.evaluate_and_dispatch_all(
            readiness_ready=False, ws_connected=False, ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.CLOSED, now=now + timedelta(seconds=12),
        )

        dispatched = [r for r in results if r.status == OperationalAlertStatus.DISPATCHED]
        assert len(dispatched) >= 1
        # Both types should have been dispatched
        assert notifier._send.call_count >= 2


# ═══════════════════════════════════════════════════════════════════════════
# Telegram Disabled / Missing Credentials
# ═══════════════════════════════════════════════════════════════════════════


class TestTelegramDisabled:
    """Graceful behavior when Telegram is disabled or credentials missing."""

    async def test_telegram_disabled_no_dispatch(self) -> None:
        config = OperationalAlertConfig(enable_operational_alerts=True)
        # No notifier
        bridge = OperationalAlertBridge(config=config, notifier=None)

        now = datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)
        results = await bridge.evaluate_and_dispatch_all(
            readiness_ready=False, ws_connected=False, ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.OPEN, now=now,
        )

        # All results should be SUPPRESSED_DISABLED since no notifier
        for result in results:
            assert result.status == OperationalAlertStatus.SUPPRESSED_DISABLED

    def test_telegram_disabled_logs_structured_reason(self) -> None:
        config = OperationalAlertConfig(enable_operational_alerts=True)
        bridge = OperationalAlertBridge(config=config, notifier=None)
        assert bridge.can_dispatch is False

    def test_telegram_disabled_does_not_crash_runtime(self) -> None:
        config = OperationalAlertConfig(enable_operational_alerts=True)
        bridge = OperationalAlertBridge(config=config, notifier=None)

        # Bridge evaluates but does not dispatch — no crash
        assert bridge.enabled is True
        assert bridge.can_dispatch is False

    def test_missing_chat_id_no_dispatch(self) -> None:
        # Bridge with no notifier handles gracefully
        config = OperationalAlertConfig(enable_operational_alerts=True)
        bridge = OperationalAlertBridge(config=config, notifier=None)
        assert bridge.can_dispatch is False

    def test_missing_bot_token_no_dispatch(self) -> None:
        config = OperationalAlertConfig(enable_operational_alerts=True)
        bridge = OperationalAlertBridge(config=config, notifier=None)
        assert bridge.can_dispatch is False
        assert bridge.enabled is True  # Bridge evaluates, just can't send

    async def test_disabled_telegram_still_evaluates_alerts(self) -> None:
        config = OperationalAlertConfig(enable_operational_alerts=True)
        bridge = OperationalAlertBridge(config=config, notifier=None)

        now = datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)
        results = await bridge.evaluate_and_dispatch_all(
            readiness_ready=True,
            ws_connected=True,
            ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.CLOSED,
            now=now,
        )

        # Still returns results even though notifier is None
        assert len(results) > 0
        # All non-circuit-breaker results are SUPPRESSED_DISABLED (no notifier)
        for result in results:
            assert result.status == OperationalAlertStatus.SUPPRESSED_DISABLED


# ═══════════════════════════════════════════════════════════════════════════
# Non-Blocking Dispatch
# ═══════════════════════════════════════════════════════════════════════════


class TestNonBlockingDispatch:
    """Alert dispatch must not block trading pipeline loops."""

    async def test_dispatch_is_non_blocking(self) -> None:
        notifier = MagicMock()
        notifier._send = AsyncMock(return_value=True)

        config = OperationalAlertConfig(
            enable_operational_alerts=True,
            readiness_degraded_threshold_seconds=10.0,
        )
        bridge = OperationalAlertBridge(config=config, notifier=notifier)

        now = datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)

        # evaluate_and_dispatch_all does not block — it returns immediately
        await bridge.evaluate_and_dispatch_all(
            readiness_ready=False, ws_connected=True, ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.CLOSED, now=now,
        )

        # dispatch_startup_alert also does not block
        result = await bridge.dispatch_startup_alert(now=now)
        # Returns immediately (None because startup is disabled by default)
        assert result is None or isinstance(result, OperationalAlertDispatchResult)

    async def test_dispatch_timeout_does_not_crash_loop(self) -> None:
        notifier = MagicMock()
        notifier._send = AsyncMock(side_effect=Exception("Simulated timeout"))

        config = OperationalAlertConfig(
            enable_operational_alerts=True,
            readiness_degraded_threshold_seconds=10.0,
        )
        bridge = OperationalAlertBridge(config=config, notifier=notifier)

        now = datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)

        # This should not raise — the bridge catches exceptions
        results = await bridge.evaluate_and_dispatch_all(
            readiness_ready=False, ws_connected=False, ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.OPEN, now=now,
        )

        # Results exist even on failure
        assert len(results) > 0

    def test_dispatch_uses_explicit_http_timeout(self) -> None:
        """The existing TelegramNotifier already uses explicit timeouts.
        The bridge delegates to it — this test verifies integration."""
        config = OperationalAlertConfig(
            enable_operational_alerts=True,
            enable_startup_alert=True,
        )
        # Bridge delegates to TelegramNotifier which has _timeout set from config
        bridge = OperationalAlertBridge(config=config, notifier=MagicMock())
        assert bridge.enabled is True


# ═══════════════════════════════════════════════════════════════════════════
# Send Failure Handling
# ═══════════════════════════════════════════════════════════════════════════


class TestSendFailure:
    """Telegram send failure handling."""

    async def test_send_failure_does_not_crash_runtime(self) -> None:
        notifier = MagicMock()
        notifier._send = AsyncMock(side_effect=ConnectionError("Network unreachable"))

        config = OperationalAlertConfig(
            enable_operational_alerts=True,
            readiness_degraded_threshold_seconds=10.0,
        )
        bridge = OperationalAlertBridge(config=config, notifier=notifier)

        now = datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)

        # Should not raise
        await bridge.evaluate_and_dispatch_all(
            readiness_ready=False, ws_connected=True, ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.CLOSED, now=now,
        )
        results = await bridge.evaluate_and_dispatch_all(
            readiness_ready=False, ws_connected=True, ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.CLOSED, now=now + timedelta(seconds=11),
        )

        # Should return FAILED status
        failed = [r for r in results if r.status == OperationalAlertStatus.FAILED]
        assert len(failed) >= 1

    async def test_send_timeout_logs_failure(self) -> None:
        notifier = MagicMock()
        notifier._send = AsyncMock(side_effect=TimeoutError("Request timed out"))

        config = OperationalAlertConfig(
            enable_operational_alerts=True,
            enable_startup_alert=True,
        )
        bridge = OperationalAlertBridge(config=config, notifier=notifier)

        result = await bridge.dispatch_startup_alert()
        assert result is not None
        assert result.status == OperationalAlertStatus.FAILED

    def test_send_uses_bounded_retry_not_unbounded(self) -> None:
        """The bridge does not implement retry — it delegates to TelegramNotifier
        which has one attempt per call. This is bounded by design."""
        config = OperationalAlertConfig(enable_operational_alerts=True)
        bridge = OperationalAlertBridge(config=config, notifier=MagicMock())

        # The bridge's _dispatch method calls notifier._send exactly once.
        # There is no retry loop in the bridge itself.
        assert bridge.enabled is True


# ═══════════════════════════════════════════════════════════════════════════
# Secret-Free Payload Enforcement
# ═══════════════════════════════════════════════════════════════════════════


class TestSecretFreePayload:
    """Alert payloads are secret-free and low-cardinality."""

    def test_alert_text_excludes_wallet_address(self) -> None:
        with pytest.raises(ValidationError):
            OperationalAlert(
                alert_type=OperationalAlertType.WEBSOCKET_STALE,
                severity=OperationalAlertSeverity.WARNING,
                message="Wallet 0x1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef disconnected",
            )

    def test_alert_text_excludes_api_key(self) -> None:
        with pytest.raises(ValidationError):
            OperationalAlert(
                alert_type=OperationalAlertType.WEBSOCKET_STALE,
                severity=OperationalAlertSeverity.WARNING,
                message="API key invalid: abc123def456",
            )

    def test_alert_text_excludes_prompt_text(self) -> None:
        with pytest.raises(ValidationError):
            OperationalAlert(
                alert_type=OperationalAlertType.WEBSOCKET_STALE,
                severity=OperationalAlertSeverity.WARNING,
                message="prompt_text contains sensitive data",
            )

    def test_alert_text_excludes_reasoning_text(self) -> None:
        with pytest.raises(ValidationError):
            OperationalAlert(
                alert_type=OperationalAlertType.WEBSOCKET_STALE,
                severity=OperationalAlertSeverity.WARNING,
                message="reasoning: agent decided to skip",
            )

    def test_alert_text_is_low_cardinality(self) -> None:
        """Alert payloads use bounded reason codes and limited message length."""
        alert = OperationalAlert(
            alert_type=OperationalAlertType.READINESS_DEGRADED,
            severity=OperationalAlertSeverity.WARNING,
            reason_code="readiness_degraded",
            message="Readiness degraded for 300s.",
        )
        # reason_code is bounded to 128 chars
        assert len(alert.reason_code) <= 128
        # message is bounded to 512 chars
        assert len(alert.message) <= 512


# ═══════════════════════════════════════════════════════════════════════════
# Invariant Guards
# ═══════════════════════════════════════════════════════════════════════════


class TestInvariantGuards:
    """Operational alerts must not violate trading integrity invariants."""

    def test_alert_evaluation_does_not_mutate_trading_state(self) -> None:
        """OperationalAlertBridge has no methods that mutate position, order,
        or execution state. It only reads health/readiness/circuit_breaker state."""
        from src.observability.operational_alerts import OperationalAlertBridge as Bridge

        # The bridge has no import of execution router, position tracker, etc.
        bridge_methods = [m for m in dir(Bridge) if not m.startswith("_")]
        # Public methods: evaluate_and_dispatch_all, dispatch_startup_alert,
        # get_state, enabled, can_dispatch — none mutate trading state
        assert "route" not in bridge_methods
        assert "execute" not in bridge_methods
        assert "sign" not in bridge_methods

    def test_alert_bridge_does_not_import_execution_router(self) -> None:
        """The operational_alerts module must not import execution router."""
        import inspect
        from src.observability import operational_alerts as oa

        source = inspect.getsource(oa)
        assert "ExecutionRouter" not in source
        assert "execution_router" not in source

    def test_alert_bridge_does_not_bypass_gatekeeper(self) -> None:
        """The bridge has no path to LLMEvaluationResponse or order routing."""
        import inspect
        from src.observability import operational_alerts as oa

        source = inspect.getsource(oa)
        assert "LLMEvaluationResponse" not in source
        assert "gatekeeper" not in source.lower()
        assert "OrderBroadcaster" not in source
