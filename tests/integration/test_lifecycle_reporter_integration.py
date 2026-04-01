"""
tests/integration/test_lifecycle_reporter_integration.py

RED-phase integration tests for WI-24 Position Lifecycle Reporter.
"""

from __future__ import annotations

import ast
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import importlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import event

from src.db.models import Position
from src.db.repositories.position_repository import PositionRepository
from src.orchestrator import Orchestrator


REPORTER_MODULE_NAME = "src.agents.execution.lifecycle_reporter"
REPORTER_MODULE_PATH = Path("src/agents/execution/lifecycle_reporter.py")

FORBIDDEN_IMPORT_PREFIXES = (
    "src.agents.context",
    "src.agents.evaluation",
    "src.agents.ingestion",
)
FORBIDDEN_IMPORTS = {
    "src.agents.execution.exit_strategy_engine",
    "src.agents.execution.exit_order_router",
    "src.agents.execution.pnl_calculator",
    "src.agents.execution.execution_router",
    "src.agents.execution.order_broadcaster",
    "src.agents.execution.signer",
    "src.agents.execution.bankroll_sync",
    "src.agents.execution.portfolio_aggregator",
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


def _make_position(
    *,
    position_id: str,
    status: str,
    entry_price: Decimal,
    order_size_usdc: Decimal,
    realized_pnl: Decimal | None,
    routed_at_utc: datetime,
    closed_at_utc: datetime | None = None,
    exit_price: Decimal | None = None,
) -> Position:
    return Position(
        id=position_id,
        condition_id=f"condition-{position_id}",
        token_id=f"token-{position_id}",
        status=status,
        side="BUY",
        entry_price=entry_price,
        order_size_usdc=order_size_usdc,
        kelly_fraction=Decimal("0.10"),
        best_ask_at_entry=entry_price,
        bankroll_usdc_at_entry=Decimal("1000"),
        execution_action="EXECUTED",
        reason=None,
        routed_at_utc=routed_at_utc,
        recorded_at_utc=routed_at_utc,
        realized_pnl=realized_pnl,
        exit_price=exit_price,
        closed_at_utc=closed_at_utc,
    )


@pytest.mark.asyncio
async def test_position_repository_get_all_positions_returns_all(db_session_factory):
    now = datetime.now(timezone.utc)

    async with db_session_factory() as session:
        repo = PositionRepository(session)
        await repo.insert_position(
            _make_position(
                position_id="all-open",
                status="OPEN",
                entry_price=Decimal("0.40"),
                order_size_usdc=Decimal("8"),
                realized_pnl=None,
                routed_at_utc=now - timedelta(hours=3),
            )
        )
        await repo.insert_position(
            _make_position(
                position_id="all-closed",
                status="CLOSED",
                entry_price=Decimal("0.50"),
                order_size_usdc=Decimal("10"),
                realized_pnl=Decimal("1"),
                routed_at_utc=now - timedelta(hours=4),
                closed_at_utc=now - timedelta(hours=1),
                exit_price=Decimal("0.55"),
            )
        )
        await session.commit()

    async with db_session_factory() as session:
        repo = PositionRepository(session)
        positions = await repo.get_all_positions()

    assert {position.id for position in positions} == {"all-open", "all-closed"}


@pytest.mark.asyncio
async def test_position_repository_get_settled_positions_filters_closed_non_null_pnl(
    db_session_factory,
):
    now = datetime.now(timezone.utc)
    async with db_session_factory() as session:
        repo = PositionRepository(session)
        await repo.insert_position(
            _make_position(
                position_id="settled-yes",
                status="CLOSED",
                entry_price=Decimal("0.45"),
                order_size_usdc=Decimal("10"),
                realized_pnl=Decimal("1.2"),
                routed_at_utc=now - timedelta(days=1),
                closed_at_utc=now - timedelta(hours=18),
                exit_price=Decimal("0.52"),
            )
        )
        await repo.insert_position(
            _make_position(
                position_id="settled-no-pnl",
                status="CLOSED",
                entry_price=Decimal("0.55"),
                order_size_usdc=Decimal("10"),
                realized_pnl=None,
                routed_at_utc=now - timedelta(days=2),
                closed_at_utc=now - timedelta(days=1, hours=12),
                exit_price=Decimal("0.60"),
            )
        )
        await repo.insert_position(
            _make_position(
                position_id="settled-open",
                status="OPEN",
                entry_price=Decimal("0.55"),
                order_size_usdc=Decimal("10"),
                realized_pnl=None,
                routed_at_utc=now - timedelta(hours=2),
            )
        )
        await session.commit()

    async with db_session_factory() as session:
        repo = PositionRepository(session)
        settled = await repo.get_settled_positions()

    assert [position.id for position in settled] == ["settled-yes"]


@pytest.mark.asyncio
async def test_position_repository_get_positions_by_status_open(db_session_factory):
    now = datetime.now(timezone.utc)
    async with db_session_factory() as session:
        repo = PositionRepository(session)
        await repo.insert_position(
            _make_position(
                position_id="status-open-1",
                status="OPEN",
                entry_price=Decimal("0.31"),
                order_size_usdc=Decimal("5"),
                realized_pnl=None,
                routed_at_utc=now - timedelta(hours=1),
            )
        )
        await repo.insert_position(
            _make_position(
                position_id="status-closed-1",
                status="CLOSED",
                entry_price=Decimal("0.62"),
                order_size_usdc=Decimal("10"),
                realized_pnl=Decimal("0.3"),
                routed_at_utc=now - timedelta(hours=5),
                closed_at_utc=now - timedelta(hours=2),
                exit_price=Decimal("0.64"),
            )
        )
        await session.commit()

    async with db_session_factory() as session:
        repo = PositionRepository(session)
        open_positions = await repo.get_positions_by_status("OPEN")

    assert [position.id for position in open_positions] == ["status-open-1"]


@pytest.mark.asyncio
async def test_position_repository_get_positions_by_status_closed(db_session_factory):
    now = datetime.now(timezone.utc)
    async with db_session_factory() as session:
        repo = PositionRepository(session)
        await repo.insert_position(
            _make_position(
                position_id="status-open-2",
                status="OPEN",
                entry_price=Decimal("0.31"),
                order_size_usdc=Decimal("5"),
                realized_pnl=None,
                routed_at_utc=now - timedelta(hours=1),
            )
        )
        await repo.insert_position(
            _make_position(
                position_id="status-closed-2",
                status="CLOSED",
                entry_price=Decimal("0.62"),
                order_size_usdc=Decimal("10"),
                realized_pnl=Decimal("0.3"),
                routed_at_utc=now - timedelta(hours=5),
                closed_at_utc=now - timedelta(hours=2),
                exit_price=Decimal("0.64"),
            )
        )
        await session.commit()

    async with db_session_factory() as session:
        repo = PositionRepository(session)
        closed_positions = await repo.get_positions_by_status("CLOSED")

    assert [position.id for position in closed_positions] == ["status-closed-2"]


@pytest.mark.asyncio
async def test_generate_report_end_to_end_sqlite_with_mixed_positions(db_session_factory):
    reporter_module = _load_module(REPORTER_MODULE_NAME)
    now = datetime.now(timezone.utc)

    async with db_session_factory() as session:
        repo = PositionRepository(session)
        await repo.insert_position(
            _make_position(
                position_id="e2e-open",
                status="OPEN",
                entry_price=Decimal("0.40"),
                order_size_usdc=Decimal("8"),
                realized_pnl=None,
                routed_at_utc=now - timedelta(hours=2),
            )
        )
        await repo.insert_position(
            _make_position(
                position_id="e2e-closed-win",
                status="CLOSED",
                entry_price=Decimal("0.50"),
                order_size_usdc=Decimal("10"),
                realized_pnl=Decimal("2"),
                routed_at_utc=now - timedelta(hours=10),
                closed_at_utc=now - timedelta(hours=7),
                exit_price=Decimal("0.60"),
            )
        )
        await repo.insert_position(
            _make_position(
                position_id="e2e-closed-loss",
                status="CLOSED",
                entry_price=Decimal("0.60"),
                order_size_usdc=Decimal("12"),
                realized_pnl=Decimal("-1"),
                routed_at_utc=now - timedelta(hours=12),
                closed_at_utc=now - timedelta(hours=6),
                exit_price=Decimal("0.55"),
            )
        )
        await session.commit()

    reporter = reporter_module.PositionLifecycleReporter(
        config=SimpleNamespace(dry_run=True),
        db_session_factory=db_session_factory,
    )
    report = await reporter.generate_report()

    assert report.total_settled_count == 2
    assert report.winning_count == 1
    assert report.losing_count == 1
    assert report.breakeven_count == 0
    assert report.total_realized_pnl == Decimal("1")
    assert report.best_pnl == Decimal("2")
    assert report.worst_pnl == Decimal("-1")
    assert len(report.entries) == 3


@pytest.mark.asyncio
async def test_generate_report_performs_zero_db_writes(
    async_engine,
    db_session_factory,
):
    reporter_module = _load_module(REPORTER_MODULE_NAME)
    now = datetime.now(timezone.utc)

    async with db_session_factory() as session:
        repo = PositionRepository(session)
        await repo.insert_position(
            _make_position(
                position_id="readonly-001",
                status="CLOSED",
                entry_price=Decimal("0.55"),
                order_size_usdc=Decimal("10"),
                realized_pnl=Decimal("0.4"),
                routed_at_utc=now - timedelta(hours=6),
                closed_at_utc=now - timedelta(hours=1),
                exit_price=Decimal("0.57"),
            )
        )
        await session.commit()

    observed_sql: list[str] = []

    def _capture_sql(conn, cursor, statement, parameters, context, executemany):
        observed_sql.append(statement.strip().upper())

    event.listen(async_engine.sync_engine, "before_cursor_execute", _capture_sql)
    reporter = reporter_module.PositionLifecycleReporter(
        config=SimpleNamespace(dry_run=True),
        db_session_factory=db_session_factory,
    )

    try:
        await reporter.generate_report()
    finally:
        event.remove(async_engine.sync_engine, "before_cursor_execute", _capture_sql)

    write_ops = [
        stmt
        for stmt in observed_sql
        if stmt.startswith("INSERT")
        or stmt.startswith("UPDATE")
        or stmt.startswith("DELETE")
    ]
    assert write_ops == []


@pytest.mark.asyncio
async def test_orchestrator_constructs_position_lifecycle_reporter_in_init(
    test_config,
    db_session_factory,
):
    patches = _patch_heavy_deps(db_session_factory=db_session_factory)
    with patch.multiple("src.orchestrator", **patches):
        orchestrator = Orchestrator(test_config)

    assert hasattr(orchestrator, "lifecycle_reporter")
    assert orchestrator.lifecycle_reporter is not None


@pytest.mark.asyncio
async def test_portfolio_aggregation_loop_invokes_generate_report_after_snapshot(
    monkeypatch,
    test_config,
    db_session_factory,
):
    patches = _patch_heavy_deps(db_session_factory=db_session_factory)
    with patch.multiple("src.orchestrator", **patches):
        orchestrator = Orchestrator(test_config)

    object.__setattr__(
        orchestrator.config,
        "portfolio_aggregation_interval_sec",
        Decimal("0.01"),
    )

    call_order: list[str] = []
    sleep_calls = 0

    async def _fake_sleep(seconds: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= 2:
            raise asyncio.CancelledError

    async def _fake_snapshot():
        call_order.append("snapshot")
        return MagicMock()

    async def _fake_report():
        call_order.append("report")
        return MagicMock()

    orchestrator.portfolio_aggregator.compute_snapshot = AsyncMock(side_effect=_fake_snapshot)
    orchestrator.lifecycle_reporter.generate_report = AsyncMock(side_effect=_fake_report)
    monkeypatch.setattr("src.orchestrator.asyncio.sleep", _fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await orchestrator._portfolio_aggregation_loop()

    assert call_order == ["snapshot", "report"]


@pytest.mark.asyncio
async def test_portfolio_aggregation_loop_catches_lifecycle_report_error_independently(
    monkeypatch,
    test_config,
    db_session_factory,
):
    patches = _patch_heavy_deps(db_session_factory=db_session_factory)
    with patch.multiple("src.orchestrator", **patches):
        orchestrator = Orchestrator(test_config)

    object.__setattr__(
        orchestrator.config,
        "portfolio_aggregation_interval_sec",
        Decimal("0.01"),
    )

    sleep_calls = 0

    async def _fake_sleep(seconds: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= 3:
            raise asyncio.CancelledError

    orchestrator.portfolio_aggregator.compute_snapshot = AsyncMock(
        side_effect=[Exception("snapshot-boom"), MagicMock()]
    )
    orchestrator.lifecycle_reporter.generate_report = AsyncMock(
        side_effect=[Exception("report-boom"), MagicMock()]
    )
    mock_logger = MagicMock()

    monkeypatch.setattr("src.orchestrator.logger", mock_logger)
    monkeypatch.setattr("src.orchestrator.asyncio.sleep", _fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await orchestrator._portfolio_aggregation_loop()

    assert orchestrator.portfolio_aggregator.compute_snapshot.await_count == 2
    assert orchestrator.lifecycle_reporter.generate_report.await_count == 2
    mock_logger.error.assert_any_call(
        "portfolio_aggregation_loop.error",
        error="snapshot-boom",
    )
    mock_logger.error.assert_any_call(
        "lifecycle_report_loop.error",
        error="report-boom",
    )


def test_lifecycle_reporter_module_has_no_forbidden_imports():
    if not REPORTER_MODULE_PATH.exists():
        pytest.fail(
            "Expected lifecycle reporter implementation file at "
            "src/agents/execution/lifecycle_reporter.py.",
            pytrace=False,
        )

    tree = ast.parse(REPORTER_MODULE_PATH.read_text())
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
