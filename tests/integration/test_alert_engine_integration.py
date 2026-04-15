"""
tests/integration/test_alert_engine_integration.py

RED-phase integration tests for WI-25 AlertEngine wiring.
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


ALERT_ENGINE_MODULE_NAME = "src.agents.execution.alert_engine"
SCHEMA_MODULE_NAME = "src.schemas.risk"
ALERT_ENGINE_MODULE_PATH = Path("src/agents/execution/alert_engine.py")

FORBIDDEN_IMPORT_PREFIXES = (
    "src.agents.context",
    "src.agents.evaluation",
    "src.agents.ingestion",
    "src.db",
    "sqlalchemy",
)
FORBIDDEN_IMPORTS = {
    "src.agents.execution.portfolio_aggregator",
    "src.agents.execution.lifecycle_reporter",
    "src.agents.execution.exit_strategy_engine",
    "src.agents.execution.exit_order_router",
    "src.agents.execution.pnl_calculator",
    "src.agents.execution.execution_router",
    "src.agents.execution.order_broadcaster",
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


def _build_orchestrator(test_config, *, db_session_factory=None):
    patches = _patch_heavy_deps(db_session_factory=db_session_factory)
    with patch.multiple("src.orchestrator", **patches):
        return Orchestrator(test_config)


def _make_snapshot(
    schema_module,
    *,
    position_count: int,
    stale_count: int,
    total_unrealized_pnl: Decimal,
    dry_run: bool,
):
    snapshot_cls = getattr(schema_module, "PortfolioSnapshot", None)
    assert snapshot_cls is not None

    return snapshot_cls(
        snapshot_at_utc=datetime.now(timezone.utc),
        position_count=position_count,
        total_notional_usdc=Decimal("100"),
        total_unrealized_pnl=total_unrealized_pnl,
        total_locked_collateral_usdc=Decimal("80"),
        positions_with_stale_price=stale_count,
        dry_run=dry_run,
    )


def _make_report(
    schema_module,
    *,
    total_settled_count: int,
    losing_count: int,
    winning_count: int,
    dry_run: bool,
):
    report_cls = getattr(schema_module, "LifecycleReport", None)
    assert report_cls is not None

    breakeven_count = total_settled_count - losing_count - winning_count
    assert breakeven_count >= 0

    return report_cls(
        report_at_utc=datetime.now(timezone.utc),
        total_settled_count=total_settled_count,
        winning_count=winning_count,
        losing_count=losing_count,
        breakeven_count=breakeven_count,
        total_realized_pnl=Decimal("0"),
        avg_hold_duration_hours=Decimal("0"),
        best_pnl=Decimal("0"),
        worst_pnl=Decimal("0"),
        entries=[],
        dry_run=dry_run,
    )


def test_alert_engine_module_has_no_forbidden_imports():
    if not ALERT_ENGINE_MODULE_PATH.exists():
        pytest.fail(
            "Expected alert engine implementation file at "
            "src/agents/execution/alert_engine.py.",
            pytrace=False,
        )

    tree = ast.parse(ALERT_ENGINE_MODULE_PATH.read_text())
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
async def test_orchestrator_constructs_alert_engine_in_init(test_config):
    orchestrator = _build_orchestrator(test_config)

    assert hasattr(orchestrator, "alert_engine")
    assert orchestrator.alert_engine is not None


@pytest.mark.asyncio
async def test_portfolio_aggregation_loop_calls_evaluate_when_snapshot_and_report_succeed(
    monkeypatch,
    test_config,
):
    schema_module = _load_module(SCHEMA_MODULE_NAME)
    orchestrator = _build_orchestrator(test_config)

    object.__setattr__(
        orchestrator.config,
        "portfolio_aggregation_interval_sec",
        Decimal("0.01"),
    )

    snapshot = _make_snapshot(
        schema_module,
        position_count=2,
        stale_count=0,
        total_unrealized_pnl=Decimal("0"),
        dry_run=True,
    )
    report = _make_report(
        schema_module,
        total_settled_count=5,
        losing_count=1,
        winning_count=4,
        dry_run=True,
    )

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
    orchestrator.alert_engine = MagicMock()
    orchestrator.alert_engine.evaluate = MagicMock(return_value=[])

    mock_logger = MagicMock()
    monkeypatch.setattr("src.orchestrator.logger", mock_logger)
    monkeypatch.setattr("src.orchestrator.asyncio.sleep", _fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await orchestrator._portfolio_aggregation_loop()

    orchestrator.alert_engine.evaluate.assert_called_once_with(snapshot, report)
    mock_logger.info.assert_any_call("alert_engine.all_clear", dry_run=True)


@pytest.mark.asyncio
async def test_portfolio_aggregation_loop_does_not_call_evaluate_when_snapshot_fails(
    monkeypatch,
    test_config,
):
    schema_module = _load_module(SCHEMA_MODULE_NAME)
    orchestrator = _build_orchestrator(test_config)

    object.__setattr__(
        orchestrator.config,
        "portfolio_aggregation_interval_sec",
        Decimal("0.01"),
    )

    report = _make_report(
        schema_module,
        total_settled_count=5,
        losing_count=2,
        winning_count=3,
        dry_run=True,
    )

    sleep_calls = 0

    async def _fake_sleep(seconds: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= 2:
            raise asyncio.CancelledError

    orchestrator.portfolio_aggregator.compute_snapshot = AsyncMock(
        side_effect=Exception("snapshot-boom")
    )
    orchestrator.lifecycle_reporter.generate_report = AsyncMock(return_value=report)
    orchestrator.alert_engine = MagicMock()
    orchestrator.alert_engine.evaluate = MagicMock(return_value=[])

    mock_logger = MagicMock()
    monkeypatch.setattr("src.orchestrator.logger", mock_logger)
    monkeypatch.setattr("src.orchestrator.asyncio.sleep", _fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await orchestrator._portfolio_aggregation_loop()

    orchestrator.alert_engine.evaluate.assert_not_called()
    mock_logger.error.assert_any_call(
        "portfolio_aggregation_loop.error",
        error="snapshot-boom",
    )


@pytest.mark.asyncio
async def test_portfolio_aggregation_loop_does_not_call_evaluate_when_report_fails(
    monkeypatch,
    test_config,
):
    schema_module = _load_module(SCHEMA_MODULE_NAME)
    orchestrator = _build_orchestrator(test_config)

    object.__setattr__(
        orchestrator.config,
        "portfolio_aggregation_interval_sec",
        Decimal("0.01"),
    )

    snapshot = _make_snapshot(
        schema_module,
        position_count=3,
        stale_count=1,
        total_unrealized_pnl=Decimal("0"),
        dry_run=True,
    )

    sleep_calls = 0

    async def _fake_sleep(seconds: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= 2:
            raise asyncio.CancelledError

    orchestrator.portfolio_aggregator.compute_snapshot = AsyncMock(
        return_value=snapshot
    )
    orchestrator.lifecycle_reporter.generate_report = AsyncMock(
        side_effect=Exception("report-boom")
    )
    orchestrator.alert_engine = MagicMock()
    orchestrator.alert_engine.evaluate = MagicMock(return_value=[])

    mock_logger = MagicMock()
    monkeypatch.setattr("src.orchestrator.logger", mock_logger)
    monkeypatch.setattr("src.orchestrator.asyncio.sleep", _fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await orchestrator._portfolio_aggregation_loop()

    orchestrator.alert_engine.evaluate.assert_not_called()
    mock_logger.error.assert_any_call(
        "lifecycle_report_loop.error",
        error="report-boom",
    )


@pytest.mark.asyncio
async def test_portfolio_aggregation_loop_catches_evaluate_exception_and_continues(
    monkeypatch,
    test_config,
):
    schema_module = _load_module(SCHEMA_MODULE_NAME)
    orchestrator = _build_orchestrator(test_config)

    object.__setattr__(
        orchestrator.config,
        "portfolio_aggregation_interval_sec",
        Decimal("0.01"),
    )

    snapshot = _make_snapshot(
        schema_module,
        position_count=3,
        stale_count=0,
        total_unrealized_pnl=Decimal("0"),
        dry_run=True,
    )
    report = _make_report(
        schema_module,
        total_settled_count=5,
        losing_count=1,
        winning_count=4,
        dry_run=True,
    )

    sleep_calls = 0

    async def _fake_sleep(seconds: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= 3:
            raise asyncio.CancelledError

    orchestrator.portfolio_aggregator.compute_snapshot = AsyncMock(
        return_value=snapshot
    )
    orchestrator.lifecycle_reporter.generate_report = AsyncMock(return_value=report)
    orchestrator.alert_engine = MagicMock()
    orchestrator.alert_engine.evaluate = MagicMock(
        side_effect=[Exception("alert-boom"), []]
    )

    mock_logger = MagicMock()
    monkeypatch.setattr("src.orchestrator.logger", mock_logger)
    monkeypatch.setattr("src.orchestrator.asyncio.sleep", _fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await orchestrator._portfolio_aggregation_loop()

    assert orchestrator.alert_engine.evaluate.call_count == 2
    mock_logger.error.assert_any_call(
        "alert_engine.error",
        error="alert-boom",
    )
    mock_logger.info.assert_any_call("alert_engine.all_clear", dry_run=True)


@pytest.mark.asyncio
async def test_portfolio_aggregation_loop_logs_alerts_fired_when_alerts_exist(
    monkeypatch,
    test_config,
):
    schema_module = _load_module(SCHEMA_MODULE_NAME)
    orchestrator = _build_orchestrator(test_config)

    object.__setattr__(
        orchestrator.config,
        "portfolio_aggregation_interval_sec",
        Decimal("0.01"),
    )

    snapshot = _make_snapshot(
        schema_module,
        position_count=8,
        stale_count=4,
        total_unrealized_pnl=Decimal("-200"),
        dry_run=False,
    )
    report = _make_report(
        schema_module,
        total_settled_count=10,
        losing_count=8,
        winning_count=2,
        dry_run=False,
    )

    severity_cls = getattr(schema_module, "AlertSeverity", None)
    event_cls = getattr(schema_module, "AlertEvent", None)
    assert severity_cls is not None
    assert event_cls is not None

    alert_one = event_cls(
        alert_at_utc=datetime.now(timezone.utc),
        severity=severity_cls.CRITICAL,
        rule_name="drawdown",
        message="drawdown",
        threshold_value=Decimal("100"),
        actual_value=Decimal("-200"),
        dry_run=False,
    )
    alert_two = event_cls(
        alert_at_utc=datetime.now(timezone.utc),
        severity=severity_cls.WARNING,
        rule_name="loss_rate",
        message="loss_rate",
        threshold_value=Decimal("0.60"),
        actual_value=Decimal("0.80"),
        dry_run=False,
    )

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
    orchestrator.alert_engine = MagicMock()
    orchestrator.alert_engine.evaluate = MagicMock(return_value=[alert_one, alert_two])

    mock_logger = MagicMock()
    monkeypatch.setattr("src.orchestrator.logger", mock_logger)
    monkeypatch.setattr("src.orchestrator.asyncio.sleep", _fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await orchestrator._portfolio_aggregation_loop()

    mock_logger.warning.assert_any_call(
        "alert_engine.alerts_fired",
        alert_count=2,
        rules=["drawdown", "loss_rate"],
        severities=["CRITICAL", "WARNING"],
        dry_run=False,
    )


def test_alert_engine_evaluate_end_to_end_with_real_models():
    alert_module = _load_module(ALERT_ENGINE_MODULE_NAME)
    schema_module = _load_module(SCHEMA_MODULE_NAME)

    snapshot = _make_snapshot(
        schema_module,
        position_count=6,
        stale_count=4,
        total_unrealized_pnl=Decimal("-150"),
        dry_run=True,
    )
    report = _make_report(
        schema_module,
        total_settled_count=10,
        losing_count=8,
        winning_count=2,
        dry_run=True,
    )

    config = type(
        "Config",
        (),
        {
            "alert_drawdown_usdc": Decimal("100"),
            "alert_stale_price_pct": Decimal("0.50"),
            "alert_max_open_positions": 5,
            "alert_loss_rate_pct": Decimal("0.60"),
        },
    )()
    engine = alert_module.AlertEngine(config=config)

    alerts = engine.evaluate(snapshot, report)

    assert [alert.rule_name for alert in alerts] == [
        "drawdown",
        "stale_price",
        "max_positions",
        "loss_rate",
    ]
