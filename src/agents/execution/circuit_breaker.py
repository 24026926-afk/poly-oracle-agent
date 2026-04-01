"""
src/agents/execution/circuit_breaker.py

WI-27 synchronous in-memory global circuit breaker.
"""

from enum import Enum

import structlog

from src.core.config import AppConfig
from src.schemas.risk import AlertEvent, AlertSeverity


class CircuitBreakerState(str, Enum):
    """Binary trip state for the global entry gate."""

    CLOSED = "CLOSED"
    OPEN = "OPEN"


class CircuitBreaker:
    """Trip-and-hold latch that blocks new BUY routing after critical drawdown."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._state = CircuitBreakerState.CLOSED
        self._log = structlog.get_logger(__name__)

    def check_entry_allowed(self) -> bool:
        return self._state == CircuitBreakerState.CLOSED

    def evaluate_alerts(self, alerts: list[AlertEvent]) -> None:
        if self._config.circuit_breaker_override_closed:
            self._state = CircuitBreakerState.CLOSED
            self._log.info("circuit_breaker.override_applied")
            self._config.circuit_breaker_override_closed = False
            return

        for alert in alerts:
            if (
                alert.severity == AlertSeverity.CRITICAL
                and alert.rule_name == "drawdown"
                and self._state == CircuitBreakerState.CLOSED
            ):
                self._state = CircuitBreakerState.OPEN
                self._log.critical(
                    "circuit_breaker.tripped",
                    rule_name=alert.rule_name,
                    severity=alert.severity.value,
                    alert_message=alert.message,
                )
                break

    def reset(self) -> None:
        self._state = CircuitBreakerState.CLOSED
        self._log.info("circuit_breaker.reset")

    @property
    def state(self) -> CircuitBreakerState:
        return self._state
