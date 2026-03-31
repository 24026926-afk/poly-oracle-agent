"""
tests/unit/test_portfolio_aggregator.py

RED-phase unit tests for WI-23 Portfolio Aggregator.
"""

from __future__ import annotations

import ast
from datetime import datetime, timezone
from decimal import Decimal
import importlib
import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.config import AppConfig
from src.db.models import Position


AGGREGATOR_MODULE_NAME = "src.agents.execution.portfolio_aggregator"
SCHEMA_MODULE_NAME = "src.schemas.risk"
AGGREGATOR_MODULE_PATH = Path("src/agents/execution/portfolio_aggregator.py")

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
    token_id: str,
    entry_price: Decimal,
    order_size_usdc: Decimal,
) -> Position:
    now = datetime.now(timezone.utc)
    return Position(
        id=position_id,
        condition_id=f"condition-{position_id}",
        token_id=token_id,
        status="OPEN",
        side="BUY",
        entry_price=entry_price,
        order_size_usdc=order_size_usdc,
        kelly_fraction=Decimal("0.10"),
        best_ask_at_entry=entry_price,
        bankroll_usdc_at_entry=Decimal("1000"),
        execution_action="EXECUTED",
        reason=None,
        routed_at_utc=now,
        recorded_at_utc=now,
    )


def _build_aggregator(aggregator_module, *, dry_run: bool = True, open_positions=None):
    config = SimpleNamespace(dry_run=dry_run)
    polymarket_client = MagicMock()
    polymarket_client.fetch_order_book = AsyncMock()

    session = MagicMock()
    db_session_factory = MagicMock(return_value=_SessionCtx(session=session))

    repo = MagicMock()
    repo.get_open_positions = AsyncMock(return_value=open_positions or [])
    aggregator_module.PositionRepository = MagicMock(return_value=repo)

    aggregator = aggregator_module.PortfolioAggregator(
        config=config,
        polymarket_client=polymarket_client,
        db_session_factory=db_session_factory,
    )
    return aggregator, polymarket_client, db_session_factory, repo


def test_portfolio_snapshot_schema_exists_and_is_frozen():
    schema_module = _load_module(SCHEMA_MODULE_NAME)
    snapshot_cls = getattr(schema_module, "PortfolioSnapshot", None)

    assert snapshot_cls is not None, "Expected PortfolioSnapshot model in src.schemas.risk."
    assert {
        "snapshot_at_utc",
        "position_count",
        "total_notional_usdc",
        "total_unrealized_pnl",
        "total_locked_collateral_usdc",
        "positions_with_stale_price",
        "dry_run",
    }.issubset(snapshot_cls.model_fields.keys())

    snapshot = snapshot_cls(
        snapshot_at_utc=datetime.now(timezone.utc),
        position_count=1,
        total_notional_usdc=Decimal("12.5"),
        total_unrealized_pnl=Decimal("2.5"),
        total_locked_collateral_usdc=Decimal("10"),
        positions_with_stale_price=0,
        dry_run=True,
    )

    with pytest.raises(Exception):
        snapshot.position_count = 99


@pytest.mark.parametrize(
    "field_name",
    [
        "total_notional_usdc",
        "total_unrealized_pnl",
        "total_locked_collateral_usdc",
    ],
)
def test_portfolio_snapshot_rejects_float_financial_fields(field_name):
    schema_module = _load_module(SCHEMA_MODULE_NAME)
    snapshot_cls = getattr(schema_module, "PortfolioSnapshot", None)
    assert snapshot_cls is not None

    payload = {
        "snapshot_at_utc": datetime.now(timezone.utc),
        "position_count": 1,
        "total_notional_usdc": Decimal("12.5"),
        "total_unrealized_pnl": Decimal("2.5"),
        "total_locked_collateral_usdc": Decimal("10"),
        "positions_with_stale_price": 0,
        "dry_run": True,
    }
    payload[field_name] = 0.1

    with pytest.raises(Exception):
        snapshot_cls(**payload)


def test_app_config_includes_enable_portfolio_aggregator_bool_default_false():
    fields = AppConfig.model_fields

    assert "enable_portfolio_aggregator" in fields
    field = fields["enable_portfolio_aggregator"]
    assert field.annotation is bool
    assert field.default is False


def test_app_config_includes_interval_decimal_default_30():
    fields = AppConfig.model_fields

    assert "portfolio_aggregation_interval_sec" in fields
    field = fields["portfolio_aggregation_interval_sec"]
    assert field.annotation is Decimal
    assert field.default == Decimal("30")


def test_app_config_accepts_interval_override_from_env(monkeypatch):
    monkeypatch.setenv("PORTFOLIO_AGGREGATION_INTERVAL_SEC", "60")
    cfg = AppConfig()

    assert isinstance(cfg.portfolio_aggregation_interval_sec, Decimal)
    assert cfg.portfolio_aggregation_interval_sec == Decimal("60")


def test_portfolio_aggregator_contract_exists_with_single_public_async_method():
    aggregator_module = _load_module(AGGREGATOR_MODULE_NAME)
    aggregator_cls = getattr(aggregator_module, "PortfolioAggregator", None)

    assert aggregator_cls is not None
    assert inspect.isclass(aggregator_cls)
    assert inspect.iscoroutinefunction(aggregator_cls.compute_snapshot)

    init_params = list(inspect.signature(aggregator_cls.__init__).parameters.keys())
    assert init_params == ["self", "config", "polymarket_client", "db_session_factory"]

    public_methods = [
        name
        for name, member in inspect.getmembers(aggregator_cls, predicate=inspect.isfunction)
        if not name.startswith("_")
    ]
    assert public_methods == ["compute_snapshot"]


@pytest.mark.asyncio
async def test_compute_snapshot_zero_open_positions_returns_zero_snapshot(monkeypatch):
    aggregator_module = _load_module(AGGREGATOR_MODULE_NAME)
    mock_logger = MagicMock()
    monkeypatch.setattr(aggregator_module, "logger", mock_logger)

    aggregator, polymarket_client, _, _ = _build_aggregator(
        aggregator_module,
        open_positions=[],
    )

    snapshot = await aggregator.compute_snapshot()

    assert snapshot.position_count == 0
    assert snapshot.total_notional_usdc == Decimal("0")
    assert snapshot.total_unrealized_pnl == Decimal("0")
    assert snapshot.total_locked_collateral_usdc == Decimal("0")
    assert snapshot.positions_with_stale_price == 0
    polymarket_client.fetch_order_book.assert_not_awaited()


@pytest.mark.asyncio
async def test_compute_snapshot_one_open_position_successful_price_fetch(monkeypatch):
    aggregator_module = _load_module(AGGREGATOR_MODULE_NAME)
    mock_logger = MagicMock()
    monkeypatch.setattr(aggregator_module, "logger", mock_logger)

    position = _make_position(
        position_id="pos-001",
        token_id="token-001",
        entry_price=Decimal("0.50"),
        order_size_usdc=Decimal("10"),
    )

    aggregator, polymarket_client, _, _ = _build_aggregator(
        aggregator_module,
        open_positions=[position],
    )
    polymarket_client.fetch_order_book = AsyncMock(
        return_value=SimpleNamespace(midpoint_probability=Decimal("0.60"))
    )

    snapshot = await aggregator.compute_snapshot()

    assert snapshot.position_count == 1
    assert snapshot.total_notional_usdc == Decimal("12")
    assert snapshot.total_unrealized_pnl == Decimal("2")
    assert snapshot.total_locked_collateral_usdc == Decimal("10")
    assert snapshot.positions_with_stale_price == 0
    polymarket_client.fetch_order_book.assert_awaited_once_with("token-001")


@pytest.mark.asyncio
async def test_compute_snapshot_aggregates_multiple_positions(monkeypatch):
    aggregator_module = _load_module(AGGREGATOR_MODULE_NAME)
    mock_logger = MagicMock()
    monkeypatch.setattr(aggregator_module, "logger", mock_logger)

    pos_a = _make_position(
        position_id="pos-a",
        token_id="token-a",
        entry_price=Decimal("0.50"),
        order_size_usdc=Decimal("10"),
    )
    pos_b = _make_position(
        position_id="pos-b",
        token_id="token-b",
        entry_price=Decimal("0.25"),
        order_size_usdc=Decimal("5"),
    )

    aggregator, polymarket_client, _, _ = _build_aggregator(
        aggregator_module,
        open_positions=[pos_a, pos_b],
    )
    polymarket_client.fetch_order_book = AsyncMock(
        side_effect=[
            SimpleNamespace(midpoint_probability=Decimal("0.60")),
            SimpleNamespace(midpoint_probability=Decimal("0.20")),
        ]
    )

    snapshot = await aggregator.compute_snapshot()

    assert snapshot.position_count == 2
    assert snapshot.total_notional_usdc == Decimal("16")
    assert snapshot.total_unrealized_pnl == Decimal("1")
    assert snapshot.total_locked_collateral_usdc == Decimal("15")


@pytest.mark.asyncio
async def test_compute_snapshot_fallback_to_entry_price_when_fetch_returns_none(
    monkeypatch,
):
    aggregator_module = _load_module(AGGREGATOR_MODULE_NAME)
    mock_logger = MagicMock()
    monkeypatch.setattr(aggregator_module, "logger", mock_logger)

    position = _make_position(
        position_id="pos-stale",
        token_id="token-stale",
        entry_price=Decimal("0.40"),
        order_size_usdc=Decimal("8"),
    )
    aggregator, polymarket_client, _, _ = _build_aggregator(
        aggregator_module,
        open_positions=[position],
    )
    polymarket_client.fetch_order_book = AsyncMock(return_value=None)

    snapshot = await aggregator.compute_snapshot()

    assert snapshot.position_count == 1
    assert snapshot.positions_with_stale_price == 1
    assert snapshot.total_unrealized_pnl == Decimal("0")
    mock_logger.warning.assert_any_call(
        "portfolio.price_fetch_failed",
        position_id="pos-stale",
        token_id="token-stale",
        fallback="entry_price",
    )


@pytest.mark.asyncio
async def test_compute_snapshot_when_all_price_fetches_fail(monkeypatch):
    aggregator_module = _load_module(AGGREGATOR_MODULE_NAME)
    mock_logger = MagicMock()
    monkeypatch.setattr(aggregator_module, "logger", mock_logger)

    positions = [
        _make_position(
            position_id="pos-1",
            token_id="token-1",
            entry_price=Decimal("0.5"),
            order_size_usdc=Decimal("10"),
        ),
        _make_position(
            position_id="pos-2",
            token_id="token-2",
            entry_price=Decimal("0.25"),
            order_size_usdc=Decimal("5"),
        ),
    ]

    aggregator, polymarket_client, _, _ = _build_aggregator(
        aggregator_module,
        open_positions=positions,
    )
    polymarket_client.fetch_order_book = AsyncMock(side_effect=[None, None])

    snapshot = await aggregator.compute_snapshot()

    assert snapshot.position_count == 2
    assert snapshot.positions_with_stale_price == 2
    assert snapshot.total_unrealized_pnl == Decimal("0")


@pytest.mark.asyncio
async def test_compute_snapshot_handles_zero_entry_price_without_division_error(
    monkeypatch,
):
    aggregator_module = _load_module(AGGREGATOR_MODULE_NAME)
    mock_logger = MagicMock()
    monkeypatch.setattr(aggregator_module, "logger", mock_logger)

    position = _make_position(
        position_id="pos-zero",
        token_id="token-zero",
        entry_price=Decimal("0"),
        order_size_usdc=Decimal("7"),
    )

    aggregator, polymarket_client, _, _ = _build_aggregator(
        aggregator_module,
        open_positions=[position],
    )
    polymarket_client.fetch_order_book = AsyncMock(
        return_value=SimpleNamespace(midpoint_probability=Decimal("0.75"))
    )

    snapshot = await aggregator.compute_snapshot()

    assert snapshot.position_count == 1
    assert snapshot.total_notional_usdc == Decimal("0")
    assert snapshot.total_unrealized_pnl == Decimal("0")
    assert snapshot.total_locked_collateral_usdc == Decimal("7")


@pytest.mark.asyncio
async def test_compute_snapshot_profitable_position_has_positive_unrealized_pnl(
    monkeypatch,
):
    aggregator_module = _load_module(AGGREGATOR_MODULE_NAME)
    mock_logger = MagicMock()
    monkeypatch.setattr(aggregator_module, "logger", mock_logger)

    position = _make_position(
        position_id="pos-profit",
        token_id="token-profit",
        entry_price=Decimal("0.40"),
        order_size_usdc=Decimal("20"),
    )

    aggregator, polymarket_client, _, _ = _build_aggregator(
        aggregator_module,
        open_positions=[position],
    )
    polymarket_client.fetch_order_book = AsyncMock(
        return_value=SimpleNamespace(midpoint_probability=Decimal("0.50"))
    )

    snapshot = await aggregator.compute_snapshot()

    assert snapshot.total_unrealized_pnl > Decimal("0")


@pytest.mark.asyncio
async def test_compute_snapshot_losing_position_has_negative_unrealized_pnl(monkeypatch):
    aggregator_module = _load_module(AGGREGATOR_MODULE_NAME)
    mock_logger = MagicMock()
    monkeypatch.setattr(aggregator_module, "logger", mock_logger)

    position = _make_position(
        position_id="pos-loss",
        token_id="token-loss",
        entry_price=Decimal("0.60"),
        order_size_usdc=Decimal("20"),
    )

    aggregator, polymarket_client, _, _ = _build_aggregator(
        aggregator_module,
        open_positions=[position],
    )
    polymarket_client.fetch_order_book = AsyncMock(
        return_value=SimpleNamespace(midpoint_probability=Decimal("0.40"))
    )

    snapshot = await aggregator.compute_snapshot()

    assert snapshot.total_unrealized_pnl < Decimal("0")


@pytest.mark.asyncio
async def test_snapshot_computed_structlog_event_emitted_with_required_fields(
    monkeypatch,
):
    aggregator_module = _load_module(AGGREGATOR_MODULE_NAME)
    mock_logger = MagicMock()
    monkeypatch.setattr(aggregator_module, "logger", mock_logger)

    position = _make_position(
        position_id="pos-log",
        token_id="token-log",
        entry_price=Decimal("0.50"),
        order_size_usdc=Decimal("10"),
    )

    aggregator, polymarket_client, _, _ = _build_aggregator(
        aggregator_module,
        open_positions=[position],
    )
    polymarket_client.fetch_order_book = AsyncMock(
        return_value=SimpleNamespace(midpoint_probability=Decimal("0.60"))
    )

    snapshot = await aggregator.compute_snapshot()

    mock_logger.info.assert_any_call(
        "portfolio.snapshot_computed",
        position_count=1,
        total_notional_usdc=str(snapshot.total_notional_usdc),
        total_unrealized_pnl=str(snapshot.total_unrealized_pnl),
        total_locked_collateral_usdc=str(snapshot.total_locked_collateral_usdc),
        positions_with_stale_price=0,
        dry_run=True,
    )


def test_portfolio_aggregator_module_import_boundary():
    if not AGGREGATOR_MODULE_PATH.exists():
        pytest.fail(
            "Expected implementation file at src/agents/execution/portfolio_aggregator.py.",
            pytrace=False,
        )

    tree = ast.parse(AGGREGATOR_MODULE_PATH.read_text())
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
