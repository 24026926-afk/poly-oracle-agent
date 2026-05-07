"""

Integration tests for WI-50 — Telegram Operational Alert Bridge.

Validates end-to-end: bridge initialization with orchestrator config,
secret-free alert payload construction, non-blocking dispatch through
the notifier transport, sustained threshold behavior with real-ish
timestamps, and cooldown dedupe across multiple evaluation cycles.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.agents.execution.circuit_breaker import CircuitBreakerState
from src.observability.operational_alerts import OperationalAlertBridge
from src.schemas.ops import (
    OperationalAlert,
    OperationalAlertConfig,
    OperationalAlertDispatchResult,
    OperationalAlertSeverity,
    OperationalAlertStatus,
    OperationalAlertType,
)


# ── Bridge Configuration Integration ───────────────────────────────────────


class TestBridgeConfigIntegration:
    """Bridge accepts OperationalAlertConfig and respects enable flags."""

    def test_bridge_disabled_when_config_disabled(self) -> None:
        config = OperationalAlertConfig(enable_operational_alerts=False)
        bridge = OperationalAlertBridge(config=config, notifier=MagicMock())
        assert bridge.enabled is False

    def test_bridge_enabled_when_config_enabled(self) -> None:
        config = OperationalAlertConfig(enable_operational_alerts=True)
        bridge = OperationalAlertBridge(config=config, notifier=MagicMock())
        assert bridge.enabled is True

    def test_can_dispatch_with_notifier(self) -> None:
        config = OperationalAlertConfig(enable_operational_alerts=True)
        notifier = MagicMock()
        bridge = OperationalAlertBridge(config=config, notifier=notifier)
        assert bridge.can_dispatch is True

    def test_cannot_dispatch_without_notifier(self) -> None:
        config = OperationalAlertConfig(enable_operational_alerts=True)
        bridge = OperationalAlertBridge(config=config, notifier=None)
        assert bridge.can_dispatch is False

    def test_custom_thresholds_accepted(self) -> None:
        config = OperationalAlertConfig(
            enable_operational_alerts=True,
            readiness_degraded_threshold_seconds=120.0,
            websocket_stale_threshold_seconds=180.0,
            alert_cooldown_seconds=900.0,
        )
        bridge = OperationalAlertBridge(config=config, notifier=MagicMock())
        assert bridge.enabled is True
        assert config.readiness_degraded_threshold_seconds == 120.0
        assert config.websocket_stale_threshold_seconds == 180.0
        assert config.alert_cooldown_seconds == 900.0


# ── End-to-End Alert Dispatch ──────────────────────────────────────────────


class TestEndToEndAlertDispatch:
    """Full alert evaluation + dispatch cycle."""

    async def test_readiness_degraded_full_cycle(self) -> None:
        notifier = MagicMock()
        notifier._send = AsyncMock(return_value=True)

        config = OperationalAlertConfig(
            enable_operational_alerts=True,
            readiness_degraded_threshold_seconds=60.0,
            alert_cooldown_seconds=600.0,
        )
        bridge = OperationalAlertBridge(config=config, notifier=notifier)

        now = datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)

        # 1. Degraded first seen
        results = await bridge.evaluate_and_dispatch_all(
            readiness_ready=False,
            ws_connected=True,
            ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.CLOSED,
            now=now,
        )
        assert all(
            r.status in (OperationalAlertStatus.SUPPRESSED_DISABLED,)
            for r in results
        )

        # 2. Still degraded but below threshold
        now2 = now + timedelta(seconds=30)
        results = await bridge.evaluate_and_dispatch_all(
            readiness_ready=False,
            ws_connected=True,
            ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.CLOSED,
            now=now2,
        )
        assert all(
            r.status == OperationalAlertStatus.SUPPRESSED_DISABLED
            for r in results
        )

        # 3. Sustained beyond threshold — fires
        now3 = now + timedelta(seconds=61)
        results = await bridge.evaluate_and_dispatch_all(
            readiness_ready=False,
            ws_connected=True,
            ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.CLOSED,
            now=now3,
        )
        dispatched = [
            r for r in results if r.status == OperationalAlertStatus.DISPATCHED
        ]
        assert len(dispatched) == 1
        notifier._send.assert_called_once()

        # Verify alert payload is secret-free
        alert = dispatched[0].alert
        assert alert is not None
        assert alert.alert_type == OperationalAlertType.READINESS_DEGRADED
        assert alert.severity == OperationalAlertSeverity.WARNING
        assert "readiness" in alert.message.lower()
        # Payload is low-cardinality and bounded
        assert len(alert.reason_code) <= 128
        assert len(alert.message) <= 512

    async def test_websocket_stale_full_cycle(self) -> None:
        notifier = MagicMock()
        notifier._send = AsyncMock(return_value=True)

        config = OperationalAlertConfig(
            enable_operational_alerts=True,
            websocket_stale_threshold_seconds=60.0,
            alert_cooldown_seconds=600.0,
        )
        bridge = OperationalAlertBridge(config=config, notifier=notifier)

        now = datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)

        # 1. Disconnect detected
        await bridge.evaluate_and_dispatch_all(
            readiness_ready=True,
            ws_connected=False,
            ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.CLOSED,
            now=now,
        )

        # 2. Sustained beyond threshold — fires
        now2 = now + timedelta(seconds=61)
        results = await bridge.evaluate_and_dispatch_all(
            readiness_ready=True,
            ws_connected=False,
            ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.CLOSED,
            now=now2,
        )
        dispatched = [
            r for r in results if r.status == OperationalAlertStatus.DISPATCHED
        ]
        assert len(dispatched) == 1
        assert dispatched[0].alert.alert_type == OperationalAlertType.WEBSOCKET_STALE

    async def test_circuit_breaker_full_transition_cycle(self) -> None:
        notifier = MagicMock()
        notifier._send = AsyncMock(return_value=True)

        config = OperationalAlertConfig(enable_operational_alerts=True)
        bridge = OperationalAlertBridge(config=config, notifier=notifier)

        now = datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)

        # OPEN transition
        results = await bridge.evaluate_and_dispatch_all(
            readiness_ready=True,
            ws_connected=True,
            ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.OPEN,
            now=now,
        )
        open_dispatched = [
            r for r in results
            if r.alert_type == OperationalAlertType.CIRCUIT_BREAKER_OPENED
            and r.status == OperationalAlertStatus.DISPATCHED
        ]
        assert len(open_dispatched) == 1

        # CLOSED transition (after some time)
        now2 = now + timedelta(seconds=120)
        results = await bridge.evaluate_and_dispatch_all(
            readiness_ready=True,
            ws_connected=True,
            ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.CLOSED,
            now=now2,
        )
        closed_dispatched = [
            r for r in results
            if r.alert_type == OperationalAlertType.CIRCUIT_BREAKER_CLOSED
            and r.status == OperationalAlertStatus.DISPATCHED
        ]
        assert len(closed_dispatched) == 1

        # Both alerts sent
        assert notifier._send.call_count == 2


# ── Cooldown Across Multiple Cycles ────────────────────────────────────────


class TestCooldownAcrossMultipleCycles:
    """Dedupe cooldown persists across multiple evaluation cycles."""

    async def test_three_cycles_two_dispatches(self) -> None:
        notifier = MagicMock()
        notifier._send = AsyncMock(return_value=True)

        config = OperationalAlertConfig(
            enable_operational_alerts=True,
            readiness_degraded_threshold_seconds=10.0,
            alert_cooldown_seconds=600.0,
        )
        bridge = OperationalAlertBridge(config=config, notifier=notifier)

        now = datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)

        # Cycle 1: first seen
        await bridge.evaluate_and_dispatch_all(
            readiness_ready=False, ws_connected=True, ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.CLOSED, now=now,
        )
        # Cycle 2: beyond threshold — dispatch
        await bridge.evaluate_and_dispatch_all(
            readiness_ready=False, ws_connected=True, ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.CLOSED, now=now + timedelta(seconds=11),
        )
        # Cycle 3: still degraded, inside cooldown — suppressed
        results = await bridge.evaluate_and_dispatch_all(
            readiness_ready=False, ws_connected=True, ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.CLOSED, now=now + timedelta(seconds=30),
        )
        suppressed = [
            r for r in results
            if r.status == OperationalAlertStatus.SUPPRESSED_COOLDOWN
        ]
        assert len(suppressed) == 1
        assert notifier._send.call_count == 1

    async def test_multiple_alert_types_independent_cooldowns(self) -> None:
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

        # First: degraded readiness fires
        await bridge.evaluate_and_dispatch_all(
            readiness_ready=False, ws_connected=True, ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.CLOSED, now=now,
        )
        await bridge.evaluate_and_dispatch_all(
            readiness_ready=False, ws_connected=True, ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.CLOSED, now=now + timedelta(seconds=11),
        )

        # Second: websocket stale fires (different type, independent cooldown)
        await bridge.evaluate_and_dispatch_all(
            readiness_ready=False, ws_connected=False, ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.CLOSED, now=now + timedelta(seconds=1),
        )
        results = await bridge.evaluate_and_dispatch_all(
            readiness_ready=False, ws_connected=False, ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.CLOSED, now=now + timedelta(seconds=12),
        )

        dispatched = [r for r in results if r.status == OperationalAlertStatus.DISPATCHED]
        assert len(dispatched) == 1  # websocket_stale dispatched
        assert notifier._send.call_count == 2  # readiness + websocket


# ── Edge Cases ─────────────────────────────────────────────────────────────


class TestEdgeCases:
    """Edge case handling from business logic."""

    async def test_readiness_flaps_deterministic_state(self) -> None:
        """Readiness flaps between ready/degraded: state remains deterministic."""
        config = OperationalAlertConfig(
            enable_operational_alerts=True,
            readiness_degraded_threshold_seconds=60.0,
        )
        bridge = OperationalAlertBridge(config=config, notifier=MagicMock())

        now = datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)

        # Flap: degraded
        await bridge.evaluate_and_dispatch_all(
            readiness_ready=False, ws_connected=True, ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.CLOSED, now=now,
        )
        state = bridge.get_state(OperationalAlertType.READINESS_DEGRADED)
        assert state.is_active is True

        # Flap: ready
        await bridge.evaluate_and_dispatch_all(
            readiness_ready=True, ws_connected=True, ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.CLOSED, now=now + timedelta(seconds=10),
        )
        state = bridge.get_state(OperationalAlertType.READINESS_DEGRADED)
        assert state.is_active is False
        assert state.first_seen_at_utc is None

        # Flap: degraded again — fresh first_seen
        await bridge.evaluate_and_dispatch_all(
            readiness_ready=False, ws_connected=True, ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.CLOSED, now=now + timedelta(seconds=20),
        )
        state = bridge.get_state(OperationalAlertType.READINESS_DEGRADED)
        assert state.is_active is True
        assert state.first_seen_at_utc is not None

    async def test_disabled_bridge_returns_empty_results(self) -> None:
        config = OperationalAlertConfig(enable_operational_alerts=False)
        bridge = OperationalAlertBridge(config=config, notifier=MagicMock())

        results = await bridge.evaluate_and_dispatch_all(
            readiness_ready=False, ws_connected=False, ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.OPEN,
        )
        assert results == []

    async def test_circuit_breaker_same_state_no_alert(self) -> None:
        """When circuit breaker stays OPEN across evaluations, no new alert fires."""
        notifier = MagicMock()
        notifier._send = AsyncMock(return_value=True)

        config = OperationalAlertConfig(enable_operational_alerts=True)
        bridge = OperationalAlertBridge(config=config, notifier=notifier)

        now = datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)

        # First: OPEN transition fires
        await bridge.evaluate_and_dispatch_all(
            readiness_ready=True, ws_connected=True, ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.OPEN, now=now,
        )

        # Second: still OPEN — no alert
        results = await bridge.evaluate_and_dispatch_all(
            readiness_ready=True, ws_connected=True, ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.OPEN, now=now + timedelta(seconds=30),
        )
        cb_results = [
            r for r in results
            if r.alert_type in (
                OperationalAlertType.CIRCUIT_BREAKER_OPENED,
                OperationalAlertType.CIRCUIT_BREAKER_CLOSED,
            )
        ]
        assert len(cb_results) == 0
        assert notifier._send.call_count == 1

    async def test_startup_alert_secret_free(self) -> None:
        notifier = MagicMock()
        notifier._send = AsyncMock(return_value=True)

        config = OperationalAlertConfig(
            enable_operational_alerts=True,
            enable_startup_alert=True,
        )
        bridge = OperationalAlertBridge(config=config, notifier=notifier)

        result = await bridge.dispatch_startup_alert()
        assert result is not None
        assert result.status == OperationalAlertStatus.DISPATCHED

        alert = result.alert
        # Verify message is secret-free
        assert "0x" not in alert.message  # no hex addresses
        assert "token" not in alert.reason_code.lower()  # no token references
        assert len(alert.message) <= 512
        assert len(alert.reason_code) <= 128

    async def test_all_alerts_evaluated_when_notifier_absent(self) -> None:
        """When Telegram is disabled, all alert types are still evaluated."""
        config = OperationalAlertConfig(enable_operational_alerts=True)
        bridge = OperationalAlertBridge(config=config, notifier=None)

        now = datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)
        results = await bridge.evaluate_and_dispatch_all(
            readiness_ready=False,
            ws_connected=False,
            ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.OPEN,
            now=now,
        )

        # At least readiness_degraded, websocket_stale results should be present
        alert_types = {r.alert_type for r in results}
        assert OperationalAlertType.READINESS_DEGRADED in alert_types
        assert OperationalAlertType.WEBSOCKET_STALE in alert_types
        # All non-circuit-breaker results are SUPPRESSED_DISABLED
        for r in results:
            assert r.status == OperationalAlertStatus.SUPPRESSED_DISABLED


# ── Secret-Free Alert Construction ─────────────────────────────────────────


class TestSecretFreeAlertConstruction:
    """Alerts constructed by the bridge are secret-free."""

    async def test_readiness_alert_no_secrets(self) -> None:
        notifier = MagicMock()
        notifier._send = AsyncMock(return_value=True)

        config = OperationalAlertConfig(
            enable_operational_alerts=True,
            readiness_degraded_threshold_seconds=10.0,
        )
        bridge = OperationalAlertBridge(config=config, notifier=notifier)

        now = datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)
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
        alert = dispatched[0].alert
        # Re-validate through OperationalAlert constructor
        revalidated = OperationalAlert(
            alert_type=alert.alert_type,
            severity=alert.severity,
            reason_code=alert.reason_code,
            message=alert.message,
            first_seen_at_utc=alert.first_seen_at_utc,
            duration_seconds=alert.duration_seconds,
        )
        assert revalidated == alert

    async def test_circuit_breaker_alerts_no_secrets(self) -> None:
        notifier = MagicMock()
        notifier._send = AsyncMock(return_value=True)

        config = OperationalAlertConfig(enable_operational_alerts=True)
        bridge = OperationalAlertBridge(config=config, notifier=notifier)

        now = datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)
        results = await bridge.evaluate_and_dispatch_all(
            readiness_ready=True, ws_connected=True, ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.OPEN, now=now,
        )

        dispatched = [r for r in results if r.status == OperationalAlertStatus.DISPATCHED]
        assert len(dispatched) == 1
        alert = dispatched[0].alert
        # No addresses, no token IDs, no private keys
        assert "0x" not in alert.message
        assert "0x" not in alert.reason_code


# ── Bridge State Isolation ─────────────────────────────────────────────────


class TestBridgeStateIsolation:
    """Each bridge instance maintains independent state."""

    async def test_two_bridges_independent_state(self) -> None:
        config = OperationalAlertConfig(enable_operational_alerts=True)

        bridge1 = OperationalAlertBridge(config=config, notifier=MagicMock())
        bridge2 = OperationalAlertBridge(config=config, notifier=MagicMock())

        now = datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)

        # Bridge 1 detects degraded
        await bridge1.evaluate_and_dispatch_all(
            readiness_ready=False, ws_connected=True, ws_pong_stale=False,
            circuit_breaker_state=CircuitBreakerState.CLOSED, now=now,
        )
        state1 = bridge1.get_state(OperationalAlertType.READINESS_DEGRADED)
        assert state1.is_active is True

        # Bridge 2 is independent
        state2 = bridge2.get_state(OperationalAlertType.READINESS_DEGRADED)
        assert state2.is_active is False
