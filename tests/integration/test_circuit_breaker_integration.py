"""
tests/integration/test_circuit_breaker_integration.py

RED-phase integration tests for WI-27 circuit breaker wiring.
"""

from __future__ import annotations

import ast
import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import importlib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.orchestrator import Orchestrator
from src.schemas.execution import (
    ExecutionAction,
    ExecutionResult,
    ExitOrderAction,
    ExitOrderResult,
    ExitReason,
    ExitResult,
    PositionRecord,
    PositionStatus,
)
from src.schemas.risk import (
    AlertEvent,
    AlertSeverity,
    LifecycleReport,
    PortfolioSnapshot,
)


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


def _patch_heavy_deps(*, db_session_factory=None):
    mock_w3 = MagicMock()
    mock_w3.eth = MagicMock()
    mock_w3.eth.get_transaction_count = AsyncMock(return_value=0)
    return {
        "AsyncWeb3": MagicMock(return_value=mock_w3),
        "AsyncHTTPProvider": MagicMock(),
        "AsyncSessionLocal": db_session_factory or MagicMock(),
        "engine": MagicMock(dispose=AsyncMock()),
    }


def _set_circuit_breaker_flags(
    test_config,
    *,
    enabled: bool,
    override_closed: bool = False,
) -> None:
    object.__setattr__(test_config, "enable_circuit_breaker", enabled)
    object.__setattr__(
        test_config,
        "circuit_breaker_override_closed",
        override_closed,
    )


def _build_orchestrator(test_config, *, db_session_factory=None) -> Orchestrator:
    with patch.multiple(
        "src.orchestrator",
        **_patch_heavy_deps(db_session_factory=db_session_factory),
    ):
        return Orchestrator(test_config)


def _make_evaluation(
    condition_id: str = "condition-001", token_id: str = "token-yes-001"
):
    market_context = MagicMock()
    market_context.condition_id = condition_id
    market_context.yes_token_id = token_id

    eval_resp = MagicMock()
    eval_resp.market_context = market_context
    eval_resp.recommended_action.value = "BUY"
    return eval_resp


def _make_execution_result(action: ExecutionAction) -> ExecutionResult:
    return ExecutionResult(
        action=action,
        reason=None,
        order_size_usdc=Decimal("15"),
        midpoint_probability=Decimal("0.55"),
        best_ask=Decimal("0.56"),
        bankroll_usdc=Decimal("1000"),
    )


def _make_alert(*, severity: AlertSeverity, rule_name: str, message: str) -> AlertEvent:
    return AlertEvent(
        alert_at_utc=datetime.now(timezone.utc),
        severity=severity,
        rule_name=rule_name,
        message=message,
        threshold_value=Decimal("100"),
        actual_value=Decimal("-140"),
        dry_run=True,
    )


def _make_snapshot(*, dry_run: bool) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        snapshot_at_utc=datetime.now(timezone.utc),
        position_count=2,
        total_notional_usdc=Decimal("100"),
        total_unrealized_pnl=Decimal("-140"),
        total_locked_collateral_usdc=Decimal("80"),
        positions_with_stale_price=0,
        dry_run=dry_run,
    )


def _make_report(*, dry_run: bool) -> LifecycleReport:
    return LifecycleReport(
        report_at_utc=datetime.now(timezone.utc),
        total_settled_count=4,
        winning_count=1,
        losing_count=3,
        breakeven_count=0,
        total_realized_pnl=Decimal("-20"),
        avg_hold_duration_hours=Decimal("5"),
        best_pnl=Decimal("2"),
        worst_pnl=Decimal("-9"),
        entries=[],
        dry_run=dry_run,
    )


def _make_position_record(*, position_id: str, token_id: str) -> PositionRecord:
    now = datetime.now(timezone.utc)
    return PositionRecord(
        id=position_id,
        condition_id=f"condition-{position_id}",
        token_id=token_id,
        status=PositionStatus.OPEN,
        side="BUY",
        entry_price=Decimal("0.60"),
        order_size_usdc=Decimal("30"),
        kelly_fraction=Decimal("0.10"),
        best_ask_at_entry=Decimal("0.61"),
        bankroll_usdc_at_entry=Decimal("1000"),
        execution_action=ExecutionAction.EXECUTED,
        reason=None,
        routed_at_utc=now,
        recorded_at_utc=now,
    )


def _make_exit_result(*, position_id: str) -> ExitResult:
    return ExitResult(
        position_id=position_id,
        condition_id=f"condition-{position_id}",
        should_exit=True,
        exit_reason=ExitReason.STOP_LOSS,
        entry_price=Decimal("0.60"),
        current_midpoint=Decimal("0.55"),
        current_best_bid=Decimal("0.54"),
        position_age_hours=Decimal("8"),
        unrealized_edge=Decimal("-0.05"),
        evaluated_at_utc=datetime.now(timezone.utc),
    )


def _make_exit_order_result(
    *, position_id: str, action: ExitOrderAction
) -> ExitOrderResult:
    return ExitOrderResult(
        position_id=position_id,
        condition_id=f"condition-{position_id}",
        action=action,
        reason=None,
        exit_price=Decimal("0.55"),
        order_size_usdc=Decimal("30"),
    )


async def _run_consumer_once(orchestrator: Orchestrator, item: dict) -> None:
    await orchestrator.execution_queue.put(item)
    task = asyncio.create_task(orchestrator._execution_consumer_loop())
    await asyncio.wait_for(orchestrator.execution_queue.join(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


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


@pytest.mark.asyncio
async def test_orchestrator_sets_circuit_breaker_none_when_disabled(test_config):
    _set_circuit_breaker_flags(test_config, enabled=False)
    orchestrator = _build_orchestrator(test_config)

    assert hasattr(orchestrator, "circuit_breaker")
    assert orchestrator.circuit_breaker is None


@pytest.mark.asyncio
async def test_orchestrator_constructs_circuit_breaker_when_enabled(test_config):
    _set_circuit_breaker_flags(test_config, enabled=True)
    module = _load_module(CIRCUIT_BREAKER_MODULE_NAME)
    orchestrator = _build_orchestrator(test_config)

    assert orchestrator.circuit_breaker is not None
    assert isinstance(orchestrator.circuit_breaker, module.CircuitBreaker)
    assert orchestrator.circuit_breaker.state == module.CircuitBreakerState.CLOSED


@pytest.mark.asyncio
async def test_execution_consumer_blocks_buy_when_breaker_open(
    monkeypatch,
    test_config,
):
    _set_circuit_breaker_flags(test_config, enabled=True)
    module = _load_module(CIRCUIT_BREAKER_MODULE_NAME)
    orchestrator = _build_orchestrator(test_config)

    orchestrator.broadcaster = AsyncMock()
    orchestrator.execution_router.route = AsyncMock(
        return_value=_make_execution_result(ExecutionAction.DRY_RUN)
    )
    orchestrator.position_tracker.record_execution = AsyncMock(return_value=None)
    orchestrator.circuit_breaker.evaluate_alerts(
        [
            _make_alert(
                severity=AlertSeverity.CRITICAL,
                rule_name="drawdown",
                message="Critical drawdown detected",
            )
        ]
    )
    assert orchestrator.circuit_breaker.state == module.CircuitBreakerState.OPEN

    item = {
        "snapshot_id": "snap-001",
        "evaluation": _make_evaluation(),
    }
    mock_logger = MagicMock()
    monkeypatch.setattr("src.orchestrator.logger", mock_logger)

    await _run_consumer_once(orchestrator, item)

    orchestrator.execution_router.route.assert_not_awaited()
    orchestrator.position_tracker.record_execution.assert_awaited_once()
    assert item["execution_result"].action == ExecutionAction.SKIP
    assert item["execution_result"].reason == "circuit_breaker_open"
    mock_logger.warning.assert_any_call(
        "circuit_breaker.entry_blocked",
        condition_id="condition-001",
    )


@pytest.mark.asyncio
async def test_position_tracker_receives_skip_result_when_breaker_blocks_entry(
    test_config,
):
    _set_circuit_breaker_flags(test_config, enabled=True)
    orchestrator = _build_orchestrator(test_config)

    orchestrator.broadcaster = AsyncMock()
    orchestrator.execution_router.route = AsyncMock(
        return_value=_make_execution_result(ExecutionAction.DRY_RUN)
    )
    orchestrator.position_tracker.record_execution = AsyncMock(return_value=None)
    orchestrator.circuit_breaker.evaluate_alerts(
        [
            _make_alert(
                severity=AlertSeverity.CRITICAL,
                rule_name="drawdown",
                message="Critical drawdown detected",
            )
        ]
    )

    item = {
        "snapshot_id": "snap-002",
        "evaluation": _make_evaluation(
            condition_id="condition-002", token_id="token-002"
        ),
    }

    await _run_consumer_once(orchestrator, item)

    call = orchestrator.position_tracker.record_execution.await_args
    assert call is not None
    assert call.kwargs["condition_id"] == "condition-002"
    assert call.kwargs["token_id"] == "token-002"
    assert call.kwargs["result"].action == ExecutionAction.SKIP
    assert call.kwargs["result"].reason == "circuit_breaker_open"


@pytest.mark.asyncio
async def test_execution_consumer_routes_normally_when_breaker_closed(test_config):
    _set_circuit_breaker_flags(test_config, enabled=True)
    orchestrator = _build_orchestrator(test_config)

    orchestrator.broadcaster = AsyncMock()
    orchestrator.execution_router.route = AsyncMock(
        return_value=_make_execution_result(ExecutionAction.DRY_RUN)
    )
    orchestrator.position_tracker.record_execution = AsyncMock(return_value=None)

    item = {
        "snapshot_id": "snap-003",
        "evaluation": _make_evaluation(
            condition_id="condition-003", token_id="token-003"
        ),
    }

    await _run_consumer_once(orchestrator, item)

    orchestrator.execution_router.route.assert_awaited_once()
    assert item["execution_result"].action == ExecutionAction.DRY_RUN


@pytest.mark.asyncio
async def test_execution_consumer_routes_normally_when_breaker_disabled(test_config):
    _set_circuit_breaker_flags(test_config, enabled=False)
    orchestrator = _build_orchestrator(test_config)

    orchestrator.broadcaster = AsyncMock()
    orchestrator.execution_router.route = AsyncMock(
        return_value=_make_execution_result(ExecutionAction.DRY_RUN)
    )
    orchestrator.position_tracker.record_execution = AsyncMock(return_value=None)

    item = {
        "snapshot_id": "snap-004",
        "evaluation": _make_evaluation(
            condition_id="condition-004", token_id="token-004"
        ),
    }

    await _run_consumer_once(orchestrator, item)

    orchestrator.execution_router.route.assert_awaited_once()
    assert item["execution_result"].action == ExecutionAction.DRY_RUN


@pytest.mark.asyncio
async def test_portfolio_aggregation_loop_trips_breaker_and_sends_telegram_on_trip(
    monkeypatch,
    test_config,
):
    _set_circuit_breaker_flags(test_config, enabled=True)
    module = _load_module(CIRCUIT_BREAKER_MODULE_NAME)
    orchestrator = _build_orchestrator(test_config)
    object.__setattr__(
        orchestrator.config,
        "portfolio_aggregation_interval_sec",
        Decimal("0.01"),
    )

    snapshot = _make_snapshot(dry_run=True)
    report = _make_report(dry_run=True)
    alerts = [
        _make_alert(
            severity=AlertSeverity.CRITICAL,
            rule_name="drawdown",
            message="Critical drawdown detected",
        )
    ]

    sleep_calls = 0

    async def _fake_sleep(seconds: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= 2:
            raise asyncio.CancelledError

    orchestrator.portfolio_aggregator.compute_snapshot = AsyncMock(
        return_value=snapshot
    )
    orchestrator.lifecycle_reporter.generate_report = AsyncMock(return_value=report)
    orchestrator.alert_engine.evaluate = MagicMock(return_value=alerts)
    orchestrator.telegram_notifier = MagicMock()
    orchestrator.telegram_notifier.send_alert = AsyncMock()
    orchestrator.telegram_notifier.send_execution_event = AsyncMock()

    monkeypatch.setattr("src.orchestrator.asyncio.sleep", _fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await orchestrator._portfolio_aggregation_loop()

    assert orchestrator.circuit_breaker.state == module.CircuitBreakerState.OPEN
    orchestrator.telegram_notifier.send_alert.assert_awaited_once_with(alerts[0])
    execution_call = orchestrator.telegram_notifier.send_execution_event.await_args
    assert execution_call is not None
    assert "CIRCUIT BREAKER TRIPPED" in execution_call.kwargs["summary"]
    assert execution_call.kwargs["dry_run"] is True


@pytest.mark.asyncio
async def test_portfolio_aggregation_loop_processes_override_when_no_alerts(
    monkeypatch,
    test_config,
):
    _set_circuit_breaker_flags(test_config, enabled=True)
    module = _load_module(CIRCUIT_BREAKER_MODULE_NAME)
    orchestrator = _build_orchestrator(test_config)
    object.__setattr__(
        orchestrator.config,
        "portfolio_aggregation_interval_sec",
        Decimal("0.01"),
    )

    snapshot = _make_snapshot(dry_run=True)
    report = _make_report(dry_run=True)
    sleep_calls = 0

    async def _fake_sleep(seconds: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= 2:
            raise asyncio.CancelledError

    orchestrator.circuit_breaker.evaluate_alerts(
        [
            _make_alert(
                severity=AlertSeverity.CRITICAL,
                rule_name="drawdown",
                message="Critical drawdown detected",
            )
        ]
    )
    assert orchestrator.circuit_breaker.state == module.CircuitBreakerState.OPEN
    object.__setattr__(
        orchestrator.config,
        "circuit_breaker_override_closed",
        True,
    )

    orchestrator.portfolio_aggregator.compute_snapshot = AsyncMock(
        return_value=snapshot
    )
    orchestrator.lifecycle_reporter.generate_report = AsyncMock(return_value=report)
    orchestrator.alert_engine.evaluate = MagicMock(return_value=[])
    monkeypatch.setattr("src.orchestrator.asyncio.sleep", _fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await orchestrator._portfolio_aggregation_loop()

    assert orchestrator.circuit_breaker.state == module.CircuitBreakerState.CLOSED
    assert orchestrator.config.circuit_breaker_override_closed is False


@pytest.mark.asyncio
async def test_exit_scan_loop_remains_active_when_breaker_open(
    monkeypatch,
    test_config,
):
    _set_circuit_breaker_flags(test_config, enabled=True)
    orchestrator = _build_orchestrator(test_config)
    object.__setattr__(
        orchestrator.config, "exit_scan_interval_seconds", Decimal("0.01")
    )

    sleep_calls = 0

    async def _fake_sleep(seconds: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= 2:
            raise asyncio.CancelledError

    orchestrator.circuit_breaker.evaluate_alerts(
        [
            _make_alert(
                severity=AlertSeverity.CRITICAL,
                rule_name="drawdown",
                message="Critical drawdown detected",
            )
        ]
    )
    orchestrator.exit_strategy_engine.scan_open_positions = AsyncMock(
        return_value=[_make_exit_result(position_id="pos-001")]
    )
    orchestrator._fetch_position_record = AsyncMock(
        return_value=_make_position_record(
            position_id="pos-001",
            token_id="token-pos-001",
        )
    )
    orchestrator.exit_order_router.route_exit = AsyncMock(
        return_value=_make_exit_order_result(
            position_id="pos-001",
            action=ExitOrderAction.DRY_RUN,
        )
    )
    orchestrator.pnl_calculator.settle = AsyncMock()
    monkeypatch.setattr("src.orchestrator.asyncio.sleep", _fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await orchestrator._exit_scan_loop()

    orchestrator.exit_strategy_engine.scan_open_positions.assert_awaited_once()
    orchestrator.exit_order_router.route_exit.assert_awaited_once()
    orchestrator.pnl_calculator.settle.assert_awaited_once()
