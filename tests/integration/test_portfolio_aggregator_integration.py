"""
tests/integration/test_portfolio_aggregator_integration.py

RED-phase integration tests for WI-23 Portfolio Aggregator wiring.
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

from src.agents.execution.polymarket_client import MarketSnapshot
from src.db.models import Position
from src.db.repositories.position_repository import PositionRepository
from src.orchestrator import Orchestrator


AGGREGATOR_MODULE_NAME = "src.agents.execution.portfolio_aggregator"
AGGREGATOR_MODULE_PATH = Path("src/agents/execution/portfolio_aggregator.py")


def _load_module(name: str):
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError:
        pytest.fail(f"Expected module {name} to exist.", pytrace=False)
    except Exception as exc:
        pytest.fail(f"Module {name} import failed unexpectedly: {exc!r}", pytrace=False)


def _patch_heavy_deps(*, db_session_factory=None):
    """Neutralize network-bound orchestrator constructor deps."""
    mock_w3 = MagicMock()
    mock_w3.eth = MagicMock()
    mock_w3.eth.get_transaction_count = AsyncMock(return_value=0)
    return {
        "AsyncWeb3": MagicMock(return_value=mock_w3),
        "AsyncHTTPProvider": MagicMock(),
        "AsyncSessionLocal": db_session_factory or MagicMock(),
        "engine": MagicMock(dispose=AsyncMock()),
    }


def _make_open_position(
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
        routed_at_utc=now - timedelta(hours=1),
        recorded_at_utc=now,
    )


def _make_snapshot(*, token_id: str, midpoint: Decimal) -> MarketSnapshot:
    bid = midpoint - Decimal("0.01")
    ask = midpoint + Decimal("0.01")
    return MarketSnapshot(
        token_id=token_id,
        best_bid=bid,
        best_ask=ask,
        midpoint_probability=midpoint,
        spread=ask - bid,
        fetched_at_utc=datetime.now(timezone.utc),
        source="test",
    )


@pytest.mark.asyncio
async def test_orchestrator_start_registers_portfolio_aggregator_task_when_enabled(
    monkeypatch, test_config
):
    patches = _patch_heavy_deps()
    with patch.multiple("src.orchestrator", **patches):
        orchestrator = Orchestrator(test_config)

    object.__setattr__(orchestrator.config, "enable_portfolio_aggregator", True)
    object.__setattr__(
        orchestrator.config,
        "portfolio_aggregation_interval_sec",
        Decimal("30"),
    )

    mock_http_session = MagicMock(close=AsyncMock())
    mock_httpx_client = MagicMock(aclose=AsyncMock())
    monkeypatch.setattr(
        "src.orchestrator.aiohttp.ClientSession",
        MagicMock(return_value=mock_http_session),
    )
    monkeypatch.setattr(
        "src.orchestrator.httpx.AsyncClient",
        MagicMock(return_value=mock_httpx_client),
    )

    fake_gamma_client = MagicMock()
    fake_discovery_engine = MagicMock()
    fake_discovery_engine.discover = AsyncMock(return_value=["condition-start-001"])
    fake_broadcaster = MagicMock()
    fake_aggregator = MagicMock()
    fake_aggregator.start = AsyncMock(return_value=None)
    fake_aggregator.stop = AsyncMock(return_value=None)

    monkeypatch.setattr(
        "src.orchestrator.GammaRESTClient",
        MagicMock(return_value=fake_gamma_client),
    )
    monkeypatch.setattr(
        "src.orchestrator.MarketDiscoveryEngine",
        MagicMock(return_value=fake_discovery_engine),
    )
    monkeypatch.setattr(
        "src.orchestrator.OrderBroadcaster",
        MagicMock(return_value=fake_broadcaster),
    )
    monkeypatch.setattr(
        "src.orchestrator.DataAggregator",
        MagicMock(return_value=fake_aggregator),
    )

    orchestrator.nonce_manager.initialize = AsyncMock(return_value=None)
    orchestrator.ws_client.run = AsyncMock(return_value=None)
    orchestrator.claude_client.start = AsyncMock(return_value=None)
    orchestrator.claude_client.stop = AsyncMock(return_value=None)
    orchestrator._execution_consumer_loop = AsyncMock(return_value=None)
    orchestrator._discovery_loop = AsyncMock(return_value=None)
    orchestrator._exit_scan_loop = AsyncMock(return_value=None)
    orchestrator._portfolio_aggregation_loop = AsyncMock(return_value=None)

    created_task_names: list[str] = []
    original_create_task = asyncio.create_task

    def _capture_create_task(coro, *, name=None):
        task = original_create_task(coro, name=name)
        created_task_names.append(task.get_name())
        return task

    monkeypatch.setattr("src.orchestrator.asyncio.create_task", _capture_create_task)

    await orchestrator.start()

    assert created_task_names == [
        "IngestionTask",
        "ContextTask",
        "EvaluationTask",
        "ExecutionTask",
        "DiscoveryTask",
        "ExitScanTask",
        "PortfolioAggregatorTask",
    ]


@pytest.mark.asyncio
async def test_orchestrator_start_does_not_register_portfolio_task_when_disabled(
    monkeypatch, test_config
):
    patches = _patch_heavy_deps()
    with patch.multiple("src.orchestrator", **patches):
        orchestrator = Orchestrator(test_config)

    object.__setattr__(orchestrator.config, "enable_portfolio_aggregator", False)

    mock_http_session = MagicMock(close=AsyncMock())
    mock_httpx_client = MagicMock(aclose=AsyncMock())
    monkeypatch.setattr(
        "src.orchestrator.aiohttp.ClientSession",
        MagicMock(return_value=mock_http_session),
    )
    monkeypatch.setattr(
        "src.orchestrator.httpx.AsyncClient",
        MagicMock(return_value=mock_httpx_client),
    )

    fake_gamma_client = MagicMock()
    fake_discovery_engine = MagicMock()
    fake_discovery_engine.discover = AsyncMock(return_value=["condition-start-001"])
    fake_broadcaster = MagicMock()
    fake_aggregator = MagicMock()
    fake_aggregator.start = AsyncMock(return_value=None)
    fake_aggregator.stop = AsyncMock(return_value=None)

    monkeypatch.setattr(
        "src.orchestrator.GammaRESTClient",
        MagicMock(return_value=fake_gamma_client),
    )
    monkeypatch.setattr(
        "src.orchestrator.MarketDiscoveryEngine",
        MagicMock(return_value=fake_discovery_engine),
    )
    monkeypatch.setattr(
        "src.orchestrator.OrderBroadcaster",
        MagicMock(return_value=fake_broadcaster),
    )
    monkeypatch.setattr(
        "src.orchestrator.DataAggregator",
        MagicMock(return_value=fake_aggregator),
    )

    orchestrator.nonce_manager.initialize = AsyncMock(return_value=None)
    orchestrator.ws_client.run = AsyncMock(return_value=None)
    orchestrator.claude_client.start = AsyncMock(return_value=None)
    orchestrator.claude_client.stop = AsyncMock(return_value=None)
    orchestrator._execution_consumer_loop = AsyncMock(return_value=None)
    orchestrator._discovery_loop = AsyncMock(return_value=None)
    orchestrator._exit_scan_loop = AsyncMock(return_value=None)

    created_task_names: list[str] = []
    original_create_task = asyncio.create_task

    def _capture_create_task(coro, *, name=None):
        task = original_create_task(coro, name=name)
        created_task_names.append(task.get_name())
        return task

    monkeypatch.setattr("src.orchestrator.asyncio.create_task", _capture_create_task)

    await orchestrator.start()

    assert created_task_names == [
        "IngestionTask",
        "ContextTask",
        "EvaluationTask",
        "ExecutionTask",
        "DiscoveryTask",
        "ExitScanTask",
    ]


@pytest.mark.asyncio
async def test_shutdown_cancels_portfolio_aggregator_task_cleanly(test_config):
    patches = _patch_heavy_deps()
    mock_engine = patches["engine"]

    with patch.multiple("src.orchestrator", **patches):
        orchestrator = Orchestrator(test_config)

    orchestrator._httpx_client = MagicMock(aclose=AsyncMock())
    orchestrator._http_session = MagicMock(close=AsyncMock())
    orchestrator.aggregator = AsyncMock()
    orchestrator.aggregator.stop = AsyncMock()
    orchestrator.claude_client.stop = AsyncMock()

    started = asyncio.Event()

    async def _portfolio_loop_forever():
        started.set()
        while True:
            await asyncio.sleep(3600)

    task = asyncio.create_task(
        _portfolio_loop_forever(),
        name="PortfolioAggregatorTask",
    )
    await started.wait()
    orchestrator._tasks = [task]

    with patch.multiple("src.orchestrator", engine=mock_engine):
        await orchestrator.shutdown()

    assert task.cancelled() or task.done()


@pytest.mark.asyncio
async def test_compute_snapshot_end_to_end_with_sqlite_and_mocked_market_data(
    db_session_factory,
):
    aggregator_module = _load_module(AGGREGATOR_MODULE_NAME)

    async with db_session_factory() as session:
        repo = PositionRepository(session)
        await repo.insert_position(
            _make_open_position(
                position_id="pos-i-001",
                token_id="token-i-001",
                entry_price=Decimal("0.40"),
                order_size_usdc=Decimal("10"),
            )
        )
        await repo.insert_position(
            _make_open_position(
                position_id="pos-i-002",
                token_id="token-i-002",
                entry_price=Decimal("0.25"),
                order_size_usdc=Decimal("5"),
            )
        )
        await session.commit()

    polymarket_client = MagicMock()

    async def _fetch(token_id: str):
        if token_id == "token-i-001":
            return _make_snapshot(token_id=token_id, midpoint=Decimal("0.50"))
        if token_id == "token-i-002":
            return None
        raise AssertionError("unexpected token")

    polymarket_client.fetch_order_book = AsyncMock(side_effect=_fetch)

    aggregator = aggregator_module.PortfolioAggregator(
        config=SimpleNamespace(dry_run=True),
        polymarket_client=polymarket_client,
        db_session_factory=db_session_factory,
    )

    snapshot = await aggregator.compute_snapshot()

    assert snapshot.position_count == 2
    # SQLite numeric affinity can introduce tiny decimal tails for division paths.
    assert snapshot.total_notional_usdc.quantize(Decimal("0.0001")) == Decimal(
        "17.5000"
    )
    assert snapshot.total_unrealized_pnl.quantize(Decimal("0.0001")) == Decimal(
        "2.5000"
    )
    assert snapshot.total_locked_collateral_usdc == Decimal("15")
    assert snapshot.positions_with_stale_price == 1


@pytest.mark.asyncio
async def test_portfolio_aggregation_loop_sleep_first_and_calls_compute_snapshot(
    monkeypatch, test_config
):
    patches = _patch_heavy_deps()
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
        if sleep_calls >= 2:
            raise asyncio.CancelledError

    orchestrator.portfolio_aggregator.compute_snapshot = AsyncMock(return_value=MagicMock())
    monkeypatch.setattr("src.orchestrator.asyncio.sleep", _fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await orchestrator._portfolio_aggregation_loop()

    orchestrator.portfolio_aggregator.compute_snapshot.assert_awaited()


@pytest.mark.asyncio
async def test_portfolio_aggregation_loop_catches_exception_and_continues(
    monkeypatch, test_config
):
    patches = _patch_heavy_deps()
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
        side_effect=[Exception("boom"), MagicMock()]
    )
    mock_logger = MagicMock()
    monkeypatch.setattr("src.orchestrator.logger", mock_logger)
    monkeypatch.setattr("src.orchestrator.asyncio.sleep", _fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await orchestrator._portfolio_aggregation_loop()

    assert orchestrator.portfolio_aggregator.compute_snapshot.await_count == 2
    mock_logger.error.assert_any_call(
        "portfolio_aggregation_loop.error",
        error="boom",
    )


@pytest.mark.asyncio
async def test_compute_snapshot_performs_zero_db_writes(
    async_engine,
    db_session_factory,
):
    aggregator_module = _load_module(AGGREGATOR_MODULE_NAME)

    async with db_session_factory() as session:
        repo = PositionRepository(session)
        await repo.insert_position(
            _make_open_position(
                position_id="pos-ro-001",
                token_id="token-ro-001",
                entry_price=Decimal("0.40"),
                order_size_usdc=Decimal("10"),
            )
        )
        await session.commit()

    observed_sql: list[str] = []

    def _capture_sql(conn, cursor, statement, parameters, context, executemany):
        observed_sql.append(statement.strip().upper())

    event.listen(async_engine.sync_engine, "before_cursor_execute", _capture_sql)

    polymarket_client = MagicMock()
    polymarket_client.fetch_order_book = AsyncMock(
        return_value=_make_snapshot(
            token_id="token-ro-001",
            midpoint=Decimal("0.45"),
        )
    )

    aggregator = aggregator_module.PortfolioAggregator(
        config=SimpleNamespace(dry_run=True),
        polymarket_client=polymarket_client,
        db_session_factory=db_session_factory,
    )

    try:
        await aggregator.compute_snapshot()
    finally:
        event.remove(async_engine.sync_engine, "before_cursor_execute", _capture_sql)

    write_ops = [
        stmt for stmt in observed_sql if stmt.startswith("INSERT") or stmt.startswith("UPDATE") or stmt.startswith("DELETE")
    ]
    assert write_ops == []


def test_portfolio_aggregator_module_has_no_forbidden_imports():
    if not AGGREGATOR_MODULE_PATH.exists():
        pytest.fail(
            "Expected portfolio aggregator implementation file at "
            "src/agents/execution/portfolio_aggregator.py.",
            pytrace=False,
        )

    tree = ast.parse(AGGREGATOR_MODULE_PATH.read_text())
    imported: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)

    forbidden_prefixes = (
        "src.agents.context",
        "src.agents.evaluation",
        "src.agents.ingestion",
    )
    forbidden_exact = {
        "src.agents.execution.exit_strategy_engine",
        "src.agents.execution.exit_order_router",
        "src.agents.execution.pnl_calculator",
        "src.agents.execution.execution_router",
        "src.agents.execution.order_broadcaster",
        "src.agents.execution.signer",
        "src.agents.execution.bankroll_sync",
    }

    forbidden_prefix_matches = sorted(
        module_name for module_name in imported if module_name.startswith(forbidden_prefixes)
    )
    forbidden_exact_matches = sorted(
        module_name for module_name in imported if module_name in forbidden_exact
    )
    assert forbidden_prefix_matches == []
    assert forbidden_exact_matches == []
