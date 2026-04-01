"""
tests/unit/test_circuit_breaker.py

RED-phase unit tests for WI-27 Global Circuit Breaker.
"""

from __future__ import annotations

import ast
from datetime import datetime, timezone
from decimal import Decimal
import importlib
import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.core.config import AppConfig
from src.schemas.risk import AlertEvent, AlertSeverity


CIRCUIT_BREAKER_MODULE_NAME = "src.agents.execution.circuit_breaker"
CIRCUIT_BREAKER_MODULE_PATH = Path("src/agents/execution/circuit_breaker.py")
FORBIDDEN_IMPORT_PREFIXES = (
    "src.agents.context",
    "src.agents.evaluation",
    "src.agents.ingestion",
    "src.db",
    "sqlalchemy",
)
FORBIDDEN_IMPORTS = {
    "asyncio",
    "httpx",
    "aiohttp",
    "src.agents.execution.alert_engine",
    "src.agents.execution.portfolio_aggregator",
    "src.agents.execution.lifecycle_reporter",
    "src.agents.execution.exit_strategy_engine",
    "src.agents.execution.exit_order_router",
    "src.agents.execution.pnl_calculator",
    "src.agents.execution.execution_router",
    "src.agents.execution.telegram_notifier",
    "src.agents.execution.broadcaster",
    "src.agents.execution.signer",
    "src.agents.execution.bankroll_sync",
    "src.agents.execution.polymarket_client",
}


def _load_module(name: str):
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError:
        pytest.fail(f"Expected module {name} to exist.", pytrace=False)
    except Exception as exc:
        pytest.fail(f"Module {name} import failed unexpectedly: {exc!r}", pytrace=False)


def _make_config(*, override_closed: bool = False):
    return SimpleNamespace(circuit_breaker_override_closed=override_closed)


def _make_alert(
    *,
    severity: AlertSeverity,
    rule_name: str,
    message: str,
) -> AlertEvent:
    return AlertEvent(
        alert_at_utc=datetime.now(timezone.utc),
        severity=severity,
        rule_name=rule_name,
        message=message,
        threshold_value=Decimal("100"),
        actual_value=Decimal("-140"),
        dry_run=True,
    )


def _build_breaker(module, *, override_closed: bool = False):
    breaker = module.CircuitBreaker(config=_make_config(override_closed=override_closed))
    breaker._log = MagicMock()
    return breaker


def test_circuit_breaker_module_has_no_forbidden_imports():
    if not CIRCUIT_BREAKER_MODULE_PATH.exists():
        pytest.fail(
            "Expected circuit breaker implementation file at "
            "src/agents/execution/circuit_breaker.py.",
            pytrace=False,
        )

    tree = ast.parse(CIRCUIT_BREAKER_MODULE_PATH.read_text())
    imported: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)

    forbidden_prefix_matches = sorted(
        module_name
        for module_name in imported
        if module_name.startswith(FORBIDDEN_IMPORT_PREFIXES)
    )
    forbidden_exact_matches = sorted(
        module_name for module_name in imported if module_name in FORBIDDEN_IMPORTS
    )

    assert forbidden_prefix_matches == []
    assert forbidden_exact_matches == []


def test_app_config_includes_circuit_breaker_fields_with_expected_defaults():
    fields = AppConfig.model_fields

    assert "enable_circuit_breaker" in fields
    assert fields["enable_circuit_breaker"].annotation is bool
    assert fields["enable_circuit_breaker"].default is False

    assert "circuit_breaker_override_closed" in fields
    assert fields["circuit_breaker_override_closed"].annotation is bool
    assert fields["circuit_breaker_override_closed"].default is False


def test_circuit_breaker_contract_exists_with_expected_public_sync_methods():
    module = _load_module(CIRCUIT_BREAKER_MODULE_NAME)
    state_cls = getattr(module, "CircuitBreakerState", None)
    breaker_cls = getattr(module, "CircuitBreaker", None)

    assert state_cls is not None
    assert [member.name for member in state_cls] == ["CLOSED", "OPEN"]
    assert [member.value for member in state_cls] == ["CLOSED", "OPEN"]

    assert breaker_cls is not None
    assert inspect.isclass(breaker_cls)
    assert list(inspect.signature(breaker_cls.__init__).parameters.keys()) == [
        "self",
        "config",
    ]
    assert not inspect.iscoroutinefunction(breaker_cls.check_entry_allowed)
    assert not inspect.iscoroutinefunction(breaker_cls.evaluate_alerts)
    assert not inspect.iscoroutinefunction(breaker_cls.reset)
    assert isinstance(breaker_cls.state, property)


def test_initial_state_is_closed_and_entry_allowed():
    module = _load_module(CIRCUIT_BREAKER_MODULE_NAME)
    breaker = _build_breaker(module)

    assert breaker.state == module.CircuitBreakerState.CLOSED
    assert breaker.check_entry_allowed() is True


def test_check_entry_allowed_returns_false_when_open():
    module = _load_module(CIRCUIT_BREAKER_MODULE_NAME)
    breaker = _build_breaker(module)

    breaker.evaluate_alerts(
        [
            _make_alert(
                severity=AlertSeverity.CRITICAL,
                rule_name="drawdown",
                message="Drawdown threshold exceeded",
            )
        ]
    )

    assert breaker.state == module.CircuitBreakerState.OPEN
    assert breaker.check_entry_allowed() is False


def test_evaluate_alerts_trips_on_critical_drawdown():
    module = _load_module(CIRCUIT_BREAKER_MODULE_NAME)
    breaker = _build_breaker(module)

    breaker.evaluate_alerts(
        [
            _make_alert(
                severity=AlertSeverity.CRITICAL,
                rule_name="drawdown",
                message="Critical drawdown detected",
            )
        ]
    )

    assert breaker.state == module.CircuitBreakerState.OPEN
    breaker._log.critical.assert_called_once()


@pytest.mark.parametrize(
    ("severity", "rule_name"),
    [
        (AlertSeverity.WARNING, "drawdown"),
        (AlertSeverity.CRITICAL, "stale_price"),
        (AlertSeverity.WARNING, "max_positions"),
    ],
)
def test_evaluate_alerts_ignores_non_trip_conditions(severity, rule_name):
    module = _load_module(CIRCUIT_BREAKER_MODULE_NAME)
    breaker = _build_breaker(module)

    breaker.evaluate_alerts(
        [
            _make_alert(
                severity=severity,
                rule_name=rule_name,
                message="Non-tripping alert",
            )
        ]
    )

    assert breaker.state == module.CircuitBreakerState.CLOSED
    breaker._log.critical.assert_not_called()


def test_evaluate_alerts_empty_list_does_not_change_state():
    module = _load_module(CIRCUIT_BREAKER_MODULE_NAME)
    breaker = _build_breaker(module)

    breaker.evaluate_alerts([])

    assert breaker.state == module.CircuitBreakerState.CLOSED
    breaker._log.critical.assert_not_called()


def test_evaluate_alerts_mixed_alerts_trips_on_critical_drawdown():
    module = _load_module(CIRCUIT_BREAKER_MODULE_NAME)
    breaker = _build_breaker(module)

    breaker.evaluate_alerts(
        [
            _make_alert(
                severity=AlertSeverity.WARNING,
                rule_name="max_positions",
                message="Too many positions",
            ),
            _make_alert(
                severity=AlertSeverity.CRITICAL,
                rule_name="drawdown",
                message="Critical drawdown detected",
            ),
            _make_alert(
                severity=AlertSeverity.INFO,
                rule_name="stale_price",
                message="FYI stale prices",
            ),
        ]
    )

    assert breaker.state == module.CircuitBreakerState.OPEN
    breaker._log.critical.assert_called_once()


def test_evaluate_alerts_is_idempotent_when_already_open():
    module = _load_module(CIRCUIT_BREAKER_MODULE_NAME)
    breaker = _build_breaker(module)
    alert = _make_alert(
        severity=AlertSeverity.CRITICAL,
        rule_name="drawdown",
        message="Critical drawdown detected",
    )

    breaker.evaluate_alerts([alert])
    breaker.evaluate_alerts([alert])

    assert breaker.state == module.CircuitBreakerState.OPEN
    breaker._log.critical.assert_called_once()


def test_reset_transitions_open_to_closed_and_logs():
    module = _load_module(CIRCUIT_BREAKER_MODULE_NAME)
    breaker = _build_breaker(module)

    breaker.evaluate_alerts(
        [
            _make_alert(
                severity=AlertSeverity.CRITICAL,
                rule_name="drawdown",
                message="Critical drawdown detected",
            )
        ]
    )
    breaker.reset()

    assert breaker.state == module.CircuitBreakerState.CLOSED
    breaker._log.info.assert_any_call("circuit_breaker.reset")


def test_reset_is_idempotent_when_already_closed():
    module = _load_module(CIRCUIT_BREAKER_MODULE_NAME)
    breaker = _build_breaker(module)

    breaker.reset()

    assert breaker.state == module.CircuitBreakerState.CLOSED
    breaker._log.info.assert_called_once_with("circuit_breaker.reset")


def test_override_flag_forces_closed_and_auto_resets_in_memory():
    module = _load_module(CIRCUIT_BREAKER_MODULE_NAME)
    breaker = _build_breaker(module)

    breaker.evaluate_alerts(
        [
            _make_alert(
                severity=AlertSeverity.CRITICAL,
                rule_name="drawdown",
                message="Critical drawdown detected",
            )
        ]
    )
    breaker._log.reset_mock()
    breaker._config.circuit_breaker_override_closed = True

    breaker.evaluate_alerts(
        [
            _make_alert(
                severity=AlertSeverity.CRITICAL,
                rule_name="drawdown",
                message="Override should win this cycle",
            )
        ]
    )

    assert breaker.state == module.CircuitBreakerState.CLOSED
    assert breaker._config.circuit_breaker_override_closed is False
    breaker._log.info.assert_called_once_with("circuit_breaker.override_applied")
    breaker._log.critical.assert_not_called()


def test_override_on_already_closed_breaker_logs_override_event():
    module = _load_module(CIRCUIT_BREAKER_MODULE_NAME)
    breaker = _build_breaker(module, override_closed=True)

    breaker.evaluate_alerts([])

    assert breaker.state == module.CircuitBreakerState.CLOSED
    breaker._log.info.assert_called_once_with("circuit_breaker.override_applied")


def test_tripped_event_is_logged_with_required_fields():
    module = _load_module(CIRCUIT_BREAKER_MODULE_NAME)
    breaker = _build_breaker(module)
    alert = _make_alert(
        severity=AlertSeverity.CRITICAL,
        rule_name="drawdown",
        message="Critical drawdown detected",
    )

    breaker.evaluate_alerts([alert])

    breaker._log.critical.assert_called_once_with(
        "circuit_breaker.tripped",
        rule_name="drawdown",
        severity="CRITICAL",
        alert_message="Critical drawdown detected",
    )


def test_state_property_returns_current_state():
    module = _load_module(CIRCUIT_BREAKER_MODULE_NAME)
    breaker = _build_breaker(module)

    assert breaker.state == module.CircuitBreakerState.CLOSED
    breaker.evaluate_alerts(
        [
            _make_alert(
                severity=AlertSeverity.CRITICAL,
                rule_name="drawdown",
                message="Critical drawdown detected",
            )
        ]
    )
    assert breaker.state == module.CircuitBreakerState.OPEN
