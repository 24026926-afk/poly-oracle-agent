"""
src/observability/operational_alerts.py

WI-50 — Telegram Operational Alert Bridge.

Evaluates bounded, deduplicated, secret-free operational alerts for
process lifecycle, readiness degradation, WebSocket health, and circuit
breaker transitions. Dispatch is non-blocking and best-effort.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional

import structlog

from src.agents.execution.circuit_breaker import CircuitBreakerState
from src.agents.execution.telegram_notifier import TelegramNotifier
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


# ── Severity mapping by alert type ─────────────────────────────────────────

_ALERT_TYPE_SEVERITY: dict[OperationalAlertType, OperationalAlertSeverity] = {
    OperationalAlertType.PROCESS_STARTED: OperationalAlertSeverity.INFO,
    OperationalAlertType.READINESS_DEGRADED: OperationalAlertSeverity.WARNING,
    OperationalAlertType.WEBSOCKET_STALE: OperationalAlertSeverity.WARNING,
    OperationalAlertType.CIRCUIT_BREAKER_OPENED: OperationalAlertSeverity.CRITICAL,
    OperationalAlertType.CIRCUIT_BREAKER_CLOSED: OperationalAlertSeverity.INFO,
}

# ── Human-readable alert titles ────────────────────────────────────────────

_ALERT_TYPE_TITLES: dict[OperationalAlertType, str] = {
    OperationalAlertType.PROCESS_STARTED: "Process Started",
    OperationalAlertType.READINESS_DEGRADED: "Readiness Degraded",
    OperationalAlertType.WEBSOCKET_STALE: "WebSocket Stale",
    OperationalAlertType.CIRCUIT_BREAKER_OPENED: "Circuit Breaker OPENED",
    OperationalAlertType.CIRCUIT_BREAKER_CLOSED: "Circuit Breaker CLOSED",
}


class OperationalAlertBridge:
    """Evaluates operational alert conditions and dispatches via Telegram.

    Read-only on trading state. Non-blocking dispatch. Deduplicated
    with configurable cooldown per alert type.
    """

    def __init__(
        self,
        config: OperationalAlertConfig,
        notifier: TelegramNotifier | None = None,
    ) -> None:
        self._config = config
        self._notifier = notifier
        self._log = structlog.get_logger(__name__)

        # Per-type state tracking for sustained thresholds and cooldown
        self._states: dict[OperationalAlertType, OperationalAlertState] = {
            at: OperationalAlertState(alert_type=at)
            for at in OperationalAlertType
        }

        # Track circuit breaker previous state for transition detection
        self._last_circuit_breaker_state: CircuitBreakerState = (
            CircuitBreakerState.CLOSED
        )

        if self._config.enable_operational_alerts and self._notifier is None:
            self._log.info(
                "operational_alerts.dispatch_disabled",
                reason="telegram_notifier_unavailable",
                transport="telegram",
            )

    # ── Public API ─────────────────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        """True when the bridge is configured to evaluate."""
        return self._config.enable_operational_alerts

    @property
    def can_dispatch(self) -> bool:
        """True when Telegram transport is available for dispatch."""
        return self._notifier is not None

    def get_state(self, alert_type: OperationalAlertType) -> OperationalAlertState:
        """Return current state for an alert type (read-only snapshot)."""
        return self._states[alert_type]

    async def evaluate_and_dispatch_all(
        self,
        *,
        readiness_ready: bool,
        ws_connected: bool,
        ws_pong_stale: bool,
        circuit_breaker_state: CircuitBreakerState | None,
        now: Optional[datetime] = None,
    ) -> list[OperationalAlertDispatchResult]:
        """Evaluate all alert conditions and dispatch where appropriate.

        Args:
            readiness_ready: True if /readyz reports READY.
            ws_connected: True if WebSocket is in CONNECTED state.
            ws_pong_stale: True if PONG has not been received within the
                expected window (use pong_timeout from WS config).
            circuit_breaker_state: Existing typed circuit breaker state, or
                None when the circuit breaker is disabled.
            now: UTC timestamp to use for evaluation (test injection).

        Returns:
            List of dispatch results, one per alert type evaluated.
        """
        if not self._config.enable_operational_alerts:
            return []

        now = now or datetime.now(timezone.utc)
        results: list[OperationalAlertDispatchResult] = []

        # 1. Readiness degraded
        results.append(
            await self._evaluate_readiness_degraded(readiness_ready, now)
        )

        # 2. WebSocket stale
        results.append(
            await self._evaluate_websocket_stale(ws_connected, ws_pong_stale, now)
        )

        # 3. Circuit breaker transitions
        results.extend(
            await self._evaluate_circuit_breaker(circuit_breaker_state, now)
        )

        return results

    async def dispatch_startup_alert(
        self, now: Optional[datetime] = None
    ) -> Optional[OperationalAlertDispatchResult]:
        """Send a one-shot process_started alert if enabled.

        This is separate from the periodic evaluate loop — it fires once
        at orchestrator startup.
        """
        if not self._config.enable_operational_alerts:
            return None
        if not self._config.enable_startup_alert:
            self._log.debug("operational_alerts.startup_alert_disabled")
            return None

        now = now or datetime.now(timezone.utc)
        alert = OperationalAlert(
            alert_type=OperationalAlertType.PROCESS_STARTED,
            severity=OperationalAlertSeverity.INFO,
            first_seen_at_utc=now,
            reason_code="process_started",
            message=f"Poly-Oracle Agent started. DRY_RUN active. Service: poly-oracle-agent.",
        )
        return await self._dispatch(alert, now)

    # ── Individual evaluators ──────────────────────────────────────────────

    async def _evaluate_readiness_degraded(
        self, readiness_ready: bool, now: datetime
    ) -> OperationalAlertDispatchResult:
        alert_type = OperationalAlertType.READINESS_DEGRADED
        state = self._states[alert_type]

        if readiness_ready:
            # Readiness is healthy — reset active state
            if state.is_active:
                state.is_active = False
                state.first_seen_at_utc = None
            state.last_evaluated_at_utc = now
            return OperationalAlertDispatchResult(
                alert_type=alert_type,
                status=OperationalAlertStatus.SUPPRESSED_DISABLED,
            )

        # Readiness is not ready — track sustained state
        if not state.is_active:
            state.is_active = True
            state.first_seen_at_utc = now
            state.last_evaluated_at_utc = now
            # Below threshold — don't alert yet
            return OperationalAlertDispatchResult(
                alert_type=alert_type,
                status=OperationalAlertStatus.SUPPRESSED_DISABLED,
            )

        state.last_evaluated_at_utc = now
        duration = (now - state.first_seen_at_utc).total_seconds()

        if duration < self._config.readiness_degraded_threshold_seconds:
            return OperationalAlertDispatchResult(
                alert_type=alert_type,
                status=OperationalAlertStatus.SUPPRESSED_DISABLED,
            )

        # Sustained beyond threshold — check cooldown
        if state.is_within_cooldown(self._config.alert_cooldown_seconds, now):
            return OperationalAlertDispatchResult(
                alert_type=alert_type,
                status=OperationalAlertStatus.SUPPRESSED_COOLDOWN,
            )

        alert = OperationalAlert(
            alert_type=alert_type,
            severity=OperationalAlertSeverity.WARNING,
            first_seen_at_utc=state.first_seen_at_utc,
            duration_seconds=duration,
            reason_code="readiness_degraded",
            message=(
                f"Readiness has been degraded for {int(duration)}s "
                f"(threshold: {int(self._config.readiness_degraded_threshold_seconds)}s). "
                f"Check /readyz endpoint."
            ),
        )
        return await self._dispatch(alert, now)

    async def _evaluate_websocket_stale(
        self,
        ws_connected: bool,
        ws_pong_stale: bool,
        now: datetime,
    ) -> OperationalAlertDispatchResult:
        alert_type = OperationalAlertType.WEBSOCKET_STALE
        state = self._states[alert_type]

        is_stale = not ws_connected or ws_pong_stale

        if not is_stale:
            # WebSocket is healthy — reset active state
            if state.is_active:
                state.is_active = False
                state.first_seen_at_utc = None
            state.last_evaluated_at_utc = now
            return OperationalAlertDispatchResult(
                alert_type=alert_type,
                status=OperationalAlertStatus.SUPPRESSED_DISABLED,
            )

        # WebSocket is stale — track sustained state
        if not state.is_active:
            state.is_active = True
            state.first_seen_at_utc = now
            state.last_evaluated_at_utc = now
            return OperationalAlertDispatchResult(
                alert_type=alert_type,
                status=OperationalAlertStatus.SUPPRESSED_DISABLED,
            )

        state.last_evaluated_at_utc = now

        # PONG timestamp absent: treat as unknown but count duration from
        # when we first noticed the stale condition.
        duration = (now - state.first_seen_at_utc).total_seconds()

        if duration < self._config.websocket_stale_threshold_seconds:
            return OperationalAlertDispatchResult(
                alert_type=alert_type,
                status=OperationalAlertStatus.SUPPRESSED_DISABLED,
            )

        # Sustained beyond threshold — check cooldown
        if state.is_within_cooldown(self._config.alert_cooldown_seconds, now):
            return OperationalAlertDispatchResult(
                alert_type=alert_type,
                status=OperationalAlertStatus.SUPPRESSED_COOLDOWN,
            )

        stale_reason = "ws_pong_stale" if ws_pong_stale else "ws_disconnected"
        alert = OperationalAlert(
            alert_type=alert_type,
            severity=OperationalAlertSeverity.WARNING,
            first_seen_at_utc=state.first_seen_at_utc,
            duration_seconds=duration,
            reason_code=stale_reason,
            message=(
                f"WebSocket has been stale for {int(duration)}s "
                f"(threshold: {int(self._config.websocket_stale_threshold_seconds)}s). "
                f"Reason: {stale_reason}."
            ),
        )
        return await self._dispatch(alert, now)

    async def _evaluate_circuit_breaker(
        self, circuit_breaker_state: CircuitBreakerState | None, now: datetime
    ) -> list[OperationalAlertDispatchResult]:
        results: list[OperationalAlertDispatchResult] = []

        if circuit_breaker_state is None:
            return results

        if (
            circuit_breaker_state == CircuitBreakerState.OPEN
            and self._last_circuit_breaker_state == CircuitBreakerState.CLOSED
        ):
            # Transition: CLOSED → OPEN
            alert_type = OperationalAlertType.CIRCUIT_BREAKER_OPENED
            state = self._states[alert_type]

            if state.is_within_cooldown(self._config.alert_cooldown_seconds, now):
                results.append(
                    OperationalAlertDispatchResult(
                        alert_type=alert_type,
                        status=OperationalAlertStatus.SUPPRESSED_COOLDOWN,
                    )
                )
            else:
                alert = OperationalAlert(
                    alert_type=alert_type,
                    severity=OperationalAlertSeverity.CRITICAL,
                    first_seen_at_utc=now,
                    reason_code="circuit_breaker_opened",
                    message=(
                        "Circuit breaker has OPENED. "
                        "All new BUY routing is blocked until manually reset or "
                        "recovery conditions are met."
                    ),
                )
                results.append(await self._dispatch(alert, now))

        elif (
            circuit_breaker_state == CircuitBreakerState.CLOSED
            and self._last_circuit_breaker_state == CircuitBreakerState.OPEN
        ):
            # Transition: OPEN → CLOSED
            alert_type = OperationalAlertType.CIRCUIT_BREAKER_CLOSED
            state = self._states[alert_type]

            if state.is_within_cooldown(self._config.alert_cooldown_seconds, now):
                results.append(
                    OperationalAlertDispatchResult(
                        alert_type=alert_type,
                        status=OperationalAlertStatus.SUPPRESSED_COOLDOWN,
                    )
                )
            else:
                alert = OperationalAlert(
                    alert_type=alert_type,
                    severity=OperationalAlertSeverity.INFO,
                    first_seen_at_utc=now,
                    reason_code="circuit_breaker_closed",
                    message=(
                        "Circuit breaker has returned to CLOSED. "
                        "BUY routing is re-enabled."
                    ),
                )
                results.append(await self._dispatch(alert, now))

        self._last_circuit_breaker_state = circuit_breaker_state
        return results

    # ── Dispatch ───────────────────────────────────────────────────────────

    async def _dispatch(
        self, alert: OperationalAlert, now: datetime
    ) -> OperationalAlertDispatchResult:
        """Dispatch a single alert via Telegram (non-blocking, best-effort)."""
        state = self._states[alert.alert_type]

        if self._notifier is None:
            self._log.debug(
                "operational_alerts.disabled_no_notifier",
                alert_type=alert.alert_type.value,
            )
            return OperationalAlertDispatchResult(
                alert_type=alert.alert_type,
                status=OperationalAlertStatus.SUPPRESSED_DISABLED,
                alert=alert,
            )

        severity_label = alert.severity.value
        text = (
            f"[{severity_label}] {_ALERT_TYPE_TITLES[alert.alert_type]}\n\n"
            f"{alert.message}\n\n"
            f"First seen: {alert.first_seen_at_utc.isoformat()}\n"
            f"Service: {alert.service_name}"
        )
        if alert.duration_seconds is not None:
            text += f"\nDuration: {int(alert.duration_seconds)}s"

        try:
            sent = await self._notifier._send(text)
        except Exception as exc:
            self._log.error(
                "operational_alerts.dispatch_error",
                alert_type=alert.alert_type.value,
                error=str(exc),
            )
            return OperationalAlertDispatchResult(
                alert_type=alert.alert_type,
                status=OperationalAlertStatus.FAILED,
                alert=alert,
                error_detail=str(exc)[:256],
            )

        if sent:
            state.last_dispatched_at_utc = now
            self._log.info(
                "operational_alerts.dispatched",
                alert_type=alert.alert_type.value,
                severity=alert.severity.value,
            )
            return OperationalAlertDispatchResult(
                alert_type=alert.alert_type,
                status=OperationalAlertStatus.DISPATCHED,
                alert=alert,
            )
        else:
            self._log.warning(
                "operational_alerts.send_returned_false",
                alert_type=alert.alert_type.value,
            )
            return OperationalAlertDispatchResult(
                alert_type=alert.alert_type,
                status=OperationalAlertStatus.FAILED,
                alert=alert,
                error_detail="Telegram send returned False",
            )
