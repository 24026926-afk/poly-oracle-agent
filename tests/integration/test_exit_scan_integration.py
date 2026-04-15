"""
tests/integration/test_exit_scan_integration.py

RED-phase integration tests for WI-22 periodic exit scan orchestration.
"""

from __future__ import annotations

import ast
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.agents.execution.polymarket_client import MarketSnapshot
from src.db.models import Position
from src.db.repositories.position_repository import PositionRepository
from src.orchestrator import Orchestrator


ENGINE_MODULE_PATH = Path("src/agents/execution/exit_strategy_engine.py")


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


def _make_open_position(*, position_id: str, token_id: str) -> Position:
    now = datetime.now(timezone.utc)
    return Position(
        id=position_id,
        condition_id=f"condition-{position_id}",
        token_id=token_id,
        status="OPEN",
        side="BUY",
        entry_price=Decimal("0.65"),
        order_size_usdc=Decimal("25"),
        kelly_fraction=Decimal("0.10"),
        best_ask_at_entry=Decimal("0.66"),
        bankroll_usdc_at_entry=Decimal("1000"),
        execution_action="EXECUTED",
        reason=None,
        routed_at_utc=now - timedelta(hours=2),
        recorded_at_utc=now,
    )


def _make_snapshot(*, token_id: str, midpoint: Decimal) -> MarketSnapshot:
    best_bid = midpoint - Decimal("0.01")
    best_ask = midpoint + Decimal("0.01")
    return MarketSnapshot(
        token_id=token_id,
        best_bid=best_bid,
        best_ask=best_ask,
        midpoint_probability=midpoint,
        spread=best_ask - best_bid,
        fetched_at_utc=datetime.now(timezone.utc),
        source="test_order_book",
    )


@pytest.mark.asyncio
async def test_orchestrator_start_registers_six_named_tasks_with_exit_scan(
    monkeypatch, test_config
):
    patches = _patch_heavy_deps()
    with patch.multiple("src.orchestrator", **patches):
        orchestrator = Orchestrator(test_config)

    object.__setattr__(orchestrator.config, "exit_scan_interval_seconds", Decimal("60"))

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
    fake_gamma_client.get_active_markets = AsyncMock(return_value=[])
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
async def test_shutdown_cancels_exit_scan_task_cleanly(test_config):
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

    async def _exit_scan_forever():
        started.set()
        while True:
            await asyncio.sleep(3600)

    exit_scan_task = asyncio.create_task(_exit_scan_forever(), name="ExitScanTask")
    await started.wait()
    orchestrator._tasks = [exit_scan_task]

    with patch.multiple("src.orchestrator", engine=mock_engine):
        await orchestrator.shutdown()

    assert exit_scan_task.cancelled() or exit_scan_task.done()


@pytest.mark.asyncio
async def test_full_boot_exit_scan_loop_fires_and_calls_scan(monkeypatch, test_config):
    patches = _patch_heavy_deps()
    with patch.multiple("src.orchestrator", **patches):
        orchestrator = Orchestrator(test_config)

    object.__setattr__(
        orchestrator.config, "exit_scan_interval_seconds", Decimal("0.01")
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
    fake_gamma_client.get_active_markets = AsyncMock(return_value=[])
    fake_discovery_engine = MagicMock()
    fake_discovery_engine.discover = AsyncMock(return_value=["condition-boot-001"])
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

    sleep_calls = 0

    async def _fake_sleep(seconds: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= 2:
            raise asyncio.CancelledError

    orchestrator.exit_strategy_engine.scan_open_positions = AsyncMock(return_value=[])
    monkeypatch.setattr("src.orchestrator.asyncio.sleep", _fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await orchestrator.start()

    orchestrator.exit_strategy_engine.scan_open_positions.assert_awaited()


@pytest.mark.asyncio
async def test_exit_scan_loop_dry_run_reads_positions_without_writes(
    monkeypatch, test_config, db_session_factory
):
    patches = _patch_heavy_deps(db_session_factory=db_session_factory)
    with patch.multiple("src.orchestrator", **patches):
        orchestrator = Orchestrator(test_config)

    object.__setattr__(
        orchestrator.config, "exit_scan_interval_seconds", Decimal("0.01")
    )

    async with db_session_factory() as session:
        repo = PositionRepository(session)
        await repo.insert_position(
            _make_open_position(position_id="pos-loop-001", token_id="token-loop-001")
        )
        await session.commit()

    orchestrator.exit_strategy_engine._polymarket_client.fetch_order_book = AsyncMock(
        return_value=_make_snapshot(
            token_id="token-loop-001",
            midpoint=Decimal("0.40"),
        )
    )

    sleep_calls = 0

    async def _fake_sleep(seconds: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr("src.orchestrator.asyncio.sleep", _fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await orchestrator._exit_scan_loop()

    async with db_session_factory() as verify_session:
        repo = PositionRepository(verify_session)
        refreshed = await repo.get_by_id("pos-loop-001")

    assert refreshed is not None
    assert refreshed.status == "OPEN"


def test_exit_strategy_engine_module_has_no_forbidden_imports():
    if not ENGINE_MODULE_PATH.exists():
        pytest.fail(
            "Expected exit strategy engine implementation file at "
            "src/agents/execution/exit_strategy_engine.py.",
            pytrace=False,
        )

    tree = ast.parse(ENGINE_MODULE_PATH.read_text())
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
    forbidden = sorted(
        module_name
        for module_name in imported
        if module_name.startswith(forbidden_prefixes)
    )
    assert forbidden == []
