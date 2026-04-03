"""
tests/unit/test_lifecycle_reporter.py

RED-phase unit tests for WI-24 Position Lifecycle Reporter.
"""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import importlib
import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.db.models import Position


REPORTER_MODULE_NAME = "src.agents.execution.lifecycle_reporter"
SCHEMA_MODULE_NAME = "src.schemas.risk"
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


class _SessionCtx:
    def __init__(self, *, session=None):
        self.session = session or MagicMock()

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _load_module(name: str):
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError:
        pytest.fail(f"Expected module {name} to exist.", pytrace=False)
    except Exception as exc:
        pytest.fail(f"Module {name} import failed unexpectedly: {exc!r}", pytrace=False)


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


def _build_reporter(
    reporter_module,
    *,
    dry_run: bool = True,
    all_positions: list[Position] | None = None,
):
    config = SimpleNamespace(dry_run=dry_run)
    session = MagicMock()
    db_session_factory = MagicMock(return_value=_SessionCtx(session=session))
    repo = MagicMock()
    repo.get_all_positions = AsyncMock(return_value=all_positions or [])
    reporter_module.PositionRepository = MagicMock(return_value=repo)
    reporter = reporter_module.PositionLifecycleReporter(
        config=config,
        db_session_factory=db_session_factory,
    )
    return reporter, repo, db_session_factory


def test_position_lifecycle_entry_schema_exists_and_is_frozen():
    schema_module = _load_module(SCHEMA_MODULE_NAME)
    entry_cls = getattr(schema_module, "PositionLifecycleEntry", None)

    assert entry_cls is not None
    assert {
        "position_id",
        "slug",
        "entry_price",
        "exit_price",
        "size_tokens",
        "realized_pnl",
        "status",
        "opened_at_utc",
        "settled_at_utc",
    }.issubset(entry_cls.model_fields.keys())

    entry = entry_cls(
        position_id="pos-001",
        slug="condition-pos-001",
        entry_price=Decimal("0.45"),
        exit_price=Decimal("0.55"),
        size_tokens=Decimal("22.2222"),
        realized_pnl=Decimal("2.2222"),
        status="CLOSED",
        opened_at_utc=datetime.now(timezone.utc),
        settled_at_utc=datetime.now(timezone.utc),
    )

    with pytest.raises(Exception):
        entry.status = "OPEN"


@pytest.mark.parametrize("field_name", ["entry_price", "size_tokens"])
def test_position_lifecycle_entry_rejects_float_financial_fields(field_name):
    schema_module = _load_module(SCHEMA_MODULE_NAME)
    entry_cls = getattr(schema_module, "PositionLifecycleEntry", None)
    assert entry_cls is not None

    payload = {
        "position_id": "pos-002",
        "slug": "condition-pos-002",
        "entry_price": Decimal("0.40"),
        "exit_price": Decimal("0.50"),
        "size_tokens": Decimal("10"),
        "realized_pnl": Decimal("1"),
        "status": "CLOSED",
        "opened_at_utc": datetime.now(timezone.utc),
        "settled_at_utc": datetime.now(timezone.utc),
    }
    payload[field_name] = 0.1

    with pytest.raises(Exception):
        entry_cls(**payload)


@pytest.mark.parametrize("field_name", ["exit_price", "realized_pnl"])
def test_position_lifecycle_entry_rejects_float_nullable_financial_fields(field_name):
    schema_module = _load_module(SCHEMA_MODULE_NAME)
    entry_cls = getattr(schema_module, "PositionLifecycleEntry", None)
    assert entry_cls is not None

    payload = {
        "position_id": "pos-003",
        "slug": "condition-pos-003",
        "entry_price": Decimal("0.40"),
        "exit_price": Decimal("0.50"),
        "size_tokens": Decimal("10"),
        "realized_pnl": Decimal("1"),
        "status": "CLOSED",
        "opened_at_utc": datetime.now(timezone.utc),
        "settled_at_utc": datetime.now(timezone.utc),
    }
    payload[field_name] = 0.1

    with pytest.raises(Exception):
        entry_cls(**payload)


def test_position_lifecycle_entry_accepts_none_nullable_fields_for_open_positions():
    schema_module = _load_module(SCHEMA_MODULE_NAME)
    entry_cls = getattr(schema_module, "PositionLifecycleEntry", None)
    assert entry_cls is not None

    entry = entry_cls(
        position_id="pos-004",
        slug="condition-pos-004",
        entry_price=Decimal("0.60"),
        exit_price=None,
        size_tokens=Decimal("5"),
        realized_pnl=None,
        status="OPEN",
        opened_at_utc=datetime.now(timezone.utc),
        settled_at_utc=None,
    )

    assert entry.exit_price is None
    assert entry.realized_pnl is None
    assert entry.settled_at_utc is None


def test_lifecycle_report_schema_exists_and_is_frozen():
    schema_module = _load_module(SCHEMA_MODULE_NAME)
    report_cls = getattr(schema_module, "LifecycleReport", None)
    entry_cls = getattr(schema_module, "PositionLifecycleEntry", None)

    assert report_cls is not None
    assert entry_cls is not None
    assert {
        "report_at_utc",
        "total_settled_count",
        "winning_count",
        "losing_count",
        "breakeven_count",
        "total_realized_pnl",
        "avg_hold_duration_hours",
        "best_pnl",
        "worst_pnl",
        "entries",
        "dry_run",
    }.issubset(report_cls.model_fields.keys())

    report = report_cls(
        report_at_utc=datetime.now(timezone.utc),
        total_settled_count=1,
        winning_count=1,
        losing_count=0,
        breakeven_count=0,
        total_realized_pnl=Decimal("1"),
        avg_hold_duration_hours=Decimal("2"),
        best_pnl=Decimal("1"),
        worst_pnl=Decimal("1"),
        entries=[
            entry_cls(
                position_id="pos-005",
                slug="condition-pos-005",
                entry_price=Decimal("0.45"),
                exit_price=Decimal("0.50"),
                size_tokens=Decimal("10"),
                realized_pnl=Decimal("0.5"),
                status="CLOSED",
                opened_at_utc=datetime.now(timezone.utc),
                settled_at_utc=datetime.now(timezone.utc),
            )
        ],
        dry_run=True,
    )

    with pytest.raises(Exception):
        report.total_settled_count = 999


@pytest.mark.parametrize(
    "field_name",
    ["total_realized_pnl", "avg_hold_duration_hours", "best_pnl", "worst_pnl"],
)
def test_lifecycle_report_rejects_float_financial_fields(field_name):
    schema_module = _load_module(SCHEMA_MODULE_NAME)
    report_cls = getattr(schema_module, "LifecycleReport", None)
    assert report_cls is not None

    payload = {
        "report_at_utc": datetime.now(timezone.utc),
        "total_settled_count": 1,
        "winning_count": 1,
        "losing_count": 0,
        "breakeven_count": 0,
        "total_realized_pnl": Decimal("1"),
        "avg_hold_duration_hours": Decimal("2"),
        "best_pnl": Decimal("1"),
        "worst_pnl": Decimal("1"),
        "entries": [],
        "dry_run": True,
    }
    payload[field_name] = 0.1

    with pytest.raises(Exception):
        report_cls(**payload)


def test_position_lifecycle_reporter_contract_exists_with_single_public_async_method():
    reporter_module = _load_module(REPORTER_MODULE_NAME)
    reporter_cls = getattr(reporter_module, "PositionLifecycleReporter", None)

    assert reporter_cls is not None
    assert inspect.isclass(reporter_cls)
    assert inspect.iscoroutinefunction(reporter_cls.generate_report)

    init_params = list(inspect.signature(reporter_cls.__init__).parameters.keys())
    assert init_params == ["self", "config", "db_session_factory"]

    public_methods = [
        name
        for name, member in inspect.getmembers(reporter_cls, predicate=inspect.isfunction)
        if not name.startswith("_")
    ]
    assert public_methods == ["generate_report"]

    generate_sig = inspect.signature(reporter_cls.generate_report)
    assert list(generate_sig.parameters.keys()) == ["self", "start_date", "end_date"]


@pytest.mark.asyncio
async def test_generate_report_zero_positions_returns_zero_report_and_empty_entries(
    monkeypatch,
):
    reporter_module = _load_module(REPORTER_MODULE_NAME)
    mock_logger = MagicMock()
    monkeypatch.setattr(reporter_module, "logger", mock_logger)
    reporter, repo, _ = _build_reporter(reporter_module, all_positions=[])

    report = await reporter.generate_report()

    assert report.total_settled_count == 0
    assert report.winning_count == 0
    assert report.losing_count == 0
    assert report.breakeven_count == 0
    assert report.total_realized_pnl == Decimal("0")
    assert report.avg_hold_duration_hours == Decimal("0")
    assert report.best_pnl == Decimal("0")
    assert report.worst_pnl == Decimal("0")
    assert report.entries == []
    assert report.dry_run is True
    repo.get_all_positions.assert_awaited_once()
    mock_logger.info.assert_any_call("lifecycle.report_empty", dry_run=True)


@pytest.mark.asyncio
async def test_generate_report_one_settled_position_returns_expected_aggregates(monkeypatch):
    reporter_module = _load_module(REPORTER_MODULE_NAME)
    mock_logger = MagicMock()
    monkeypatch.setattr(reporter_module, "logger", mock_logger)

    opened = datetime.now(timezone.utc) - timedelta(hours=4)
    closed = opened + timedelta(hours=2)
    positions = [
        _make_position(
            position_id="pos-settled-1",
            status="CLOSED",
            entry_price=Decimal("0.50"),
            order_size_usdc=Decimal("10"),
            realized_pnl=Decimal("2"),
            routed_at_utc=opened,
            closed_at_utc=closed,
            exit_price=Decimal("0.60"),
        )
    ]
    reporter, _, _ = _build_reporter(reporter_module, all_positions=positions)

    report = await reporter.generate_report()

    assert report.total_settled_count == 1
    assert report.winning_count == 1
    assert report.losing_count == 0
    assert report.breakeven_count == 0
    assert report.total_realized_pnl == Decimal("2")
    assert report.avg_hold_duration_hours == Decimal("2")
    assert report.best_pnl == Decimal("2")
    assert report.worst_pnl == Decimal("2")
    assert len(report.entries) == 1
    assert report.entries[0].size_tokens == Decimal("20")


@pytest.mark.asyncio
async def test_generate_report_multiple_settled_positions_aggregates_and_classifies():
    reporter_module = _load_module(REPORTER_MODULE_NAME)

    now = datetime.now(timezone.utc)
    positions = [
        _make_position(
            position_id="pos-win",
            status="CLOSED",
            entry_price=Decimal("0.50"),
            order_size_usdc=Decimal("10"),
            realized_pnl=Decimal("2"),
            routed_at_utc=now - timedelta(hours=6),
            closed_at_utc=now - timedelta(hours=3),
            exit_price=Decimal("0.60"),
        ),
        _make_position(
            position_id="pos-loss",
            status="CLOSED",
            entry_price=Decimal("0.40"),
            order_size_usdc=Decimal("8"),
            realized_pnl=Decimal("-1"),
            routed_at_utc=now - timedelta(hours=5),
            closed_at_utc=now - timedelta(hours=2),
            exit_price=Decimal("0.35"),
        ),
        _make_position(
            position_id="pos-flat",
            status="CLOSED",
            entry_price=Decimal("0.25"),
            order_size_usdc=Decimal("5"),
            realized_pnl=Decimal("0"),
            routed_at_utc=now - timedelta(hours=4),
            closed_at_utc=now - timedelta(hours=1),
            exit_price=Decimal("0.25"),
        ),
    ]
    reporter, _, _ = _build_reporter(reporter_module, all_positions=positions)

    report = await reporter.generate_report()

    assert report.total_settled_count == 3
    assert report.winning_count == 1
    assert report.losing_count == 1
    assert report.breakeven_count == 1
    assert report.total_realized_pnl == Decimal("1")
    assert report.best_pnl == Decimal("2")
    assert report.worst_pnl == Decimal("-1")
    assert report.winning_count + report.losing_count + report.breakeven_count == report.total_settled_count


@pytest.mark.asyncio
async def test_generate_report_open_positions_are_in_entries_but_not_settled_counts():
    reporter_module = _load_module(REPORTER_MODULE_NAME)
    now = datetime.now(timezone.utc)

    positions = [
        _make_position(
            position_id="pos-open",
            status="OPEN",
            entry_price=Decimal("0.33"),
            order_size_usdc=Decimal("6.6"),
            realized_pnl=None,
            routed_at_utc=now - timedelta(hours=1),
        ),
        _make_position(
            position_id="pos-closed",
            status="CLOSED",
            entry_price=Decimal("0.50"),
            order_size_usdc=Decimal("10"),
            realized_pnl=Decimal("1"),
            routed_at_utc=now - timedelta(hours=4),
            closed_at_utc=now - timedelta(hours=2),
            exit_price=Decimal("0.55"),
        ),
    ]
    reporter, _, _ = _build_reporter(reporter_module, all_positions=positions)

    report = await reporter.generate_report()

    assert report.total_settled_count == 1
    assert len(report.entries) == 2
    open_entry = next(entry for entry in report.entries if entry.position_id == "pos-open")
    assert open_entry.exit_price is None
    assert open_entry.realized_pnl is None
    assert open_entry.settled_at_utc is None


@pytest.mark.asyncio
async def test_generate_report_entry_price_zero_sets_size_tokens_zero():
    reporter_module = _load_module(REPORTER_MODULE_NAME)
    now = datetime.now(timezone.utc)
    positions = [
        _make_position(
            position_id="pos-zero",
            status="OPEN",
            entry_price=Decimal("0"),
            order_size_usdc=Decimal("9"),
            realized_pnl=None,
            routed_at_utc=now - timedelta(hours=1),
        )
    ]
    reporter, _, _ = _build_reporter(reporter_module, all_positions=positions)

    report = await reporter.generate_report()
    assert len(report.entries) == 1
    assert report.entries[0].size_tokens == Decimal("0")


@pytest.mark.asyncio
async def test_generate_report_start_date_filters_positions():
    reporter_module = _load_module(REPORTER_MODULE_NAME)
    now = datetime.now(timezone.utc)

    old_position = _make_position(
        position_id="pos-old",
        status="CLOSED",
        entry_price=Decimal("0.50"),
        order_size_usdc=Decimal("10"),
        realized_pnl=Decimal("1"),
        routed_at_utc=now - timedelta(days=3),
        closed_at_utc=now - timedelta(days=2, hours=22),
        exit_price=Decimal("0.55"),
    )
    new_position = _make_position(
        position_id="pos-new",
        status="CLOSED",
        entry_price=Decimal("0.50"),
        order_size_usdc=Decimal("10"),
        realized_pnl=Decimal("2"),
        routed_at_utc=now - timedelta(hours=10),
        closed_at_utc=now - timedelta(hours=6),
        exit_price=Decimal("0.60"),
    )
    reporter, _, _ = _build_reporter(
        reporter_module, all_positions=[old_position, new_position]
    )

    report = await reporter.generate_report(start_date=now - timedelta(days=1))
    assert [entry.position_id for entry in report.entries] == ["pos-new"]


@pytest.mark.asyncio
async def test_generate_report_end_date_filters_positions():
    reporter_module = _load_module(REPORTER_MODULE_NAME)
    now = datetime.now(timezone.utc)

    older = _make_position(
        position_id="pos-older",
        status="CLOSED",
        entry_price=Decimal("0.50"),
        order_size_usdc=Decimal("10"),
        realized_pnl=Decimal("1"),
        routed_at_utc=now - timedelta(days=2),
        closed_at_utc=now - timedelta(days=2, hours=-1),
        exit_price=Decimal("0.55"),
    )
    latest = _make_position(
        position_id="pos-latest",
        status="CLOSED",
        entry_price=Decimal("0.50"),
        order_size_usdc=Decimal("10"),
        realized_pnl=Decimal("2"),
        routed_at_utc=now - timedelta(hours=4),
        closed_at_utc=now - timedelta(hours=2),
        exit_price=Decimal("0.60"),
    )
    reporter, _, _ = _build_reporter(reporter_module, all_positions=[older, latest])

    report = await reporter.generate_report(end_date=now - timedelta(days=1))
    assert [entry.position_id for entry in report.entries] == ["pos-older"]


@pytest.mark.asyncio
async def test_generate_report_start_and_end_date_intersection_filters():
    reporter_module = _load_module(REPORTER_MODULE_NAME)
    now = datetime.now(timezone.utc)

    pos_a = _make_position(
        position_id="pos-a",
        status="CLOSED",
        entry_price=Decimal("0.40"),
        order_size_usdc=Decimal("10"),
        realized_pnl=Decimal("1"),
        routed_at_utc=now - timedelta(days=3),
        closed_at_utc=now - timedelta(days=3, hours=-1),
        exit_price=Decimal("0.44"),
    )
    pos_b = _make_position(
        position_id="pos-b",
        status="CLOSED",
        entry_price=Decimal("0.40"),
        order_size_usdc=Decimal("10"),
        realized_pnl=Decimal("2"),
        routed_at_utc=now - timedelta(days=2),
        closed_at_utc=now - timedelta(days=2, hours=-1),
        exit_price=Decimal("0.48"),
    )
    pos_c = _make_position(
        position_id="pos-c",
        status="CLOSED",
        entry_price=Decimal("0.40"),
        order_size_usdc=Decimal("10"),
        realized_pnl=Decimal("3"),
        routed_at_utc=now - timedelta(hours=12),
        closed_at_utc=now - timedelta(hours=10),
        exit_price=Decimal("0.52"),
    )
    reporter, _, _ = _build_reporter(reporter_module, all_positions=[pos_a, pos_b, pos_c])

    report = await reporter.generate_report(
        start_date=now - timedelta(days=2, hours=12),
        end_date=now - timedelta(days=1),
    )
    assert [entry.position_id for entry in report.entries] == ["pos-b"]


@pytest.mark.asyncio
async def test_generate_report_invalid_date_range_fails_open_logs_warning(monkeypatch):
    reporter_module = _load_module(REPORTER_MODULE_NAME)
    mock_logger = MagicMock()
    monkeypatch.setattr(reporter_module, "logger", mock_logger)

    now = datetime.now(timezone.utc)
    positions = [
        _make_position(
            position_id="pos-a",
            status="OPEN",
            entry_price=Decimal("0.50"),
            order_size_usdc=Decimal("10"),
            realized_pnl=None,
            routed_at_utc=now - timedelta(days=3),
        ),
        _make_position(
            position_id="pos-b",
            status="OPEN",
            entry_price=Decimal("0.50"),
            order_size_usdc=Decimal("10"),
            realized_pnl=None,
            routed_at_utc=now - timedelta(days=1),
        ),
    ]
    reporter, _, _ = _build_reporter(reporter_module, all_positions=positions)

    report = await reporter.generate_report(
        start_date=now,
        end_date=now - timedelta(days=2),
    )

    assert len(report.entries) == 2
    mock_logger.warning.assert_any_call(
        "lifecycle.invalid_date_range",
        start_date=(now.isoformat()),
        end_date=(now - timedelta(days=2)).isoformat(),
    )


@pytest.mark.asyncio
async def test_report_generated_event_contains_required_fields(monkeypatch):
    reporter_module = _load_module(REPORTER_MODULE_NAME)
    mock_logger = MagicMock()
    monkeypatch.setattr(reporter_module, "logger", mock_logger)

    now = datetime.now(timezone.utc)
    position = _make_position(
        position_id="pos-log",
        status="CLOSED",
        entry_price=Decimal("0.50"),
        order_size_usdc=Decimal("10"),
        realized_pnl=Decimal("1.5"),
        routed_at_utc=now - timedelta(hours=3),
        closed_at_utc=now - timedelta(hours=1),
        exit_price=Decimal("0.575"),
    )
    reporter, _, _ = _build_reporter(reporter_module, all_positions=[position])

    report = await reporter.generate_report()

    mock_logger.info.assert_any_call(
        "lifecycle.report_generated",
        total_settled_count=1,
        winning_count=1,
        losing_count=0,
        breakeven_count=0,
        total_realized_pnl=str(report.total_realized_pnl),
        total_gas_cost_usdc=str(report.total_gas_cost_usdc),
        total_fees_usdc=str(report.total_fees_usdc),
        total_net_realized_pnl=str(report.total_net_realized_pnl),
        avg_hold_duration_hours=str(report.avg_hold_duration_hours),
        best_pnl=str(report.best_pnl),
        worst_pnl=str(report.worst_pnl),
        entry_count=1,
        dry_run=True,
    )


def test_lifecycle_reporter_module_import_boundary():
    if not REPORTER_MODULE_PATH.exists():
        pytest.fail(
            "Expected implementation file at src/agents/execution/lifecycle_reporter.py.",
            pytrace=False,
        )

    tree = ast.parse(REPORTER_MODULE_PATH.read_text())
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported_modules.add(node.module)

    forbidden_prefix_matches = sorted(
        module_name
        for module_name in imported_modules
        if module_name.startswith(FORBIDDEN_IMPORT_PREFIXES)
    )
    forbidden_exact_matches = sorted(
        module_name for module_name in imported_modules if module_name in FORBIDDEN_IMPORTS
    )
    assert forbidden_prefix_matches == []
    assert forbidden_exact_matches == []
