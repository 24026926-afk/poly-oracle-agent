"""
tests/integration/test_exit_strategy_engine_integration.py

RED-phase integration tests for WI-19 Exit Strategy Engine.

These tests codify batch exit evaluation behavior before implementation
changes are made in src/.
"""

from __future__ import annotations

import ast
import importlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


ENGINE_MODULE_NAME = "src.agents.execution.exit_strategy_engine"
SCHEMA_MODULE_NAME = "src.schemas.execution"
MODELS_MODULE_NAME = "src.db.models"
REPO_MODULE_NAME = "src.db.repositories.position_repo"
POLYMARKET_MODULE_NAME = "src.agents.execution.polymarket_client"
ENGINE_MODULE_PATH = Path("src/agents/execution/exit_strategy_engine.py")


def _load_module(name: str):
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError:
        pytest.fail(f"Expected module {name} to exist.", pytrace=False)
    except Exception as exc:
        pytest.fail(f"Module {name} import failed unexpectedly: {exc!r}", pytrace=False)


def _load_contracts():
    return (
        _load_module(ENGINE_MODULE_NAME),
        _load_module(SCHEMA_MODULE_NAME),
        _load_module(MODELS_MODULE_NAME),
        _load_module(REPO_MODULE_NAME),
        _load_module(POLYMARKET_MODULE_NAME),
    )


def _make_config(*, dry_run: bool):
    return SimpleNamespace(
        dry_run=dry_run,
        exit_position_max_age_hours=Decimal("48"),
        exit_stop_loss_drop=Decimal("0.15"),
        exit_take_profit_gain=Decimal("0.20"),
    )


def _make_position_orm(
    models_module,
    *,
    position_id: str,
    condition_id: str,
    token_id: str,
    entry_price: Decimal = Decimal("0.65"),
    routed_at_utc: datetime | None = None,
):
    if routed_at_utc is None:
        routed_at_utc = datetime.now(timezone.utc) - timedelta(hours=1)

    return models_module.Position(
        id=position_id,
        condition_id=condition_id,
        token_id=token_id,
        status="OPEN",
        side="BUY",
        entry_price=entry_price,
        order_size_usdc=Decimal("10"),
        kelly_fraction=Decimal("0.10"),
        best_ask_at_entry=entry_price + Decimal("0.01"),
        bankroll_usdc_at_entry=Decimal("1000"),
        execution_action="EXECUTED",
        reason=None,
        routed_at_utc=routed_at_utc,
        recorded_at_utc=routed_at_utc,
    )


def _make_snapshot(
    polymarket_module,
    *,
    token_id: str,
    best_bid: Decimal,
    best_ask: Decimal,
    midpoint: Decimal,
):
    return polymarket_module.MarketSnapshot(
        token_id=token_id,
        best_bid=best_bid,
        best_ask=best_ask,
        midpoint_probability=midpoint,
        spread=best_ask - best_bid,
        fetched_at_utc=datetime.now(timezone.utc),
        source="test_order_book",
    )


@pytest.mark.asyncio
async def test_scan_open_positions_reads_db_and_calls_repository_method(
    async_session,
    db_session_factory,
    monkeypatch,
):
    (
        engine_module,
        _schema_module,
        models_module,
        repo_module,
        polymarket_module,
    ) = _load_contracts()

    repo_cls = repo_module.PositionRepository
    repo = repo_cls(async_session)
    position = _make_position_orm(
        models_module,
        position_id="pos-exit-int-001",
        condition_id="condition-exit-int-001",
        token_id="token-exit-int-001",
    )
    await repo.insert_position(position)
    await async_session.commit()

    original = repo_cls.get_open_positions
    calls = {"count": 0}

    async def _spy(self):
        calls["count"] += 1
        return await original(self)

    monkeypatch.setattr(repo_cls, "get_open_positions", _spy)

    polymarket_client = MagicMock()
    polymarket_client.fetch_order_book = AsyncMock(
        return_value=_make_snapshot(
            polymarket_module,
            token_id="token-exit-int-001",
            best_bid=Decimal("0.69"),
            best_ask=Decimal("0.71"),
            midpoint=Decimal("0.70"),
        )
    )

    engine = engine_module.ExitStrategyEngine(
        config=_make_config(dry_run=True),
        polymarket_client=polymarket_client,
        db_session_factory=db_session_factory,
    )

    results = await engine.scan_open_positions()

    assert calls["count"] == 1
    assert any(result.position_id == "pos-exit-int-001" for result in results)


@pytest.mark.asyncio
async def test_scan_open_positions_fetches_order_book_for_each_open_position(
    async_session,
    db_session_factory,
):
    (
        engine_module,
        _schema_module,
        models_module,
        repo_module,
        polymarket_module,
    ) = _load_contracts()

    repo = repo_module.PositionRepository(async_session)
    pos_a = _make_position_orm(
        models_module,
        position_id="pos-exit-int-002",
        condition_id="condition-exit-int-002",
        token_id="token-exit-int-002",
    )
    pos_b = _make_position_orm(
        models_module,
        position_id="pos-exit-int-003",
        condition_id="condition-exit-int-003",
        token_id="token-exit-int-003",
    )
    await repo.insert_position(pos_a)
    await repo.insert_position(pos_b)
    await async_session.commit()

    called_token_ids: list[str] = []

    async def _fetch_order_book(token_id: str):
        called_token_ids.append(token_id)
        midpoint = Decimal("0.70") if token_id.endswith("002") else Decimal("0.71")
        return _make_snapshot(
            polymarket_module,
            token_id=token_id,
            best_bid=midpoint - Decimal("0.01"),
            best_ask=midpoint + Decimal("0.01"),
            midpoint=midpoint,
        )

    polymarket_client = MagicMock()
    polymarket_client.fetch_order_book = AsyncMock(side_effect=_fetch_order_book)

    engine = engine_module.ExitStrategyEngine(
        config=_make_config(dry_run=True),
        polymarket_client=polymarket_client,
        db_session_factory=db_session_factory,
    )

    results = await engine.scan_open_positions()

    assert len(results) >= 2
    assert sorted(called_token_ids) == ["token-exit-int-002", "token-exit-int-003"]


@pytest.mark.asyncio
async def test_scan_open_positions_returns_stale_market_exit_when_order_book_missing(
    async_session,
    db_session_factory,
):
    (
        engine_module,
        schema_module,
        models_module,
        repo_module,
        _polymarket_module,
    ) = _load_contracts()

    repo = repo_module.PositionRepository(async_session)
    position = _make_position_orm(
        models_module,
        position_id="pos-exit-int-004",
        condition_id="condition-exit-int-004",
        token_id="token-exit-int-004",
    )
    await repo.insert_position(position)
    await async_session.commit()

    polymarket_client = MagicMock()
    polymarket_client.fetch_order_book = AsyncMock(return_value=None)

    engine = engine_module.ExitStrategyEngine(
        config=_make_config(dry_run=True),
        polymarket_client=polymarket_client,
        db_session_factory=db_session_factory,
    )

    results = await engine.scan_open_positions()

    assert len(results) == 1
    assert results[0].should_exit is True
    assert results[0].exit_reason == schema_module.ExitReason.STALE_MARKET


@pytest.mark.asyncio
async def test_full_flow_hold_keeps_position_open(
    async_session,
    db_session_factory,
):
    (
        engine_module,
        _schema_module,
        models_module,
        repo_module,
        polymarket_module,
    ) = _load_contracts()

    repo = repo_module.PositionRepository(async_session)
    position = _make_position_orm(
        models_module,
        position_id="pos-exit-int-005",
        condition_id="condition-exit-int-005",
        token_id="token-exit-int-005",
        entry_price=Decimal("0.65"),
        routed_at_utc=datetime.now(timezone.utc) - timedelta(hours=2),
    )
    await repo.insert_position(position)
    await async_session.commit()

    polymarket_client = MagicMock()
    polymarket_client.fetch_order_book = AsyncMock(
        return_value=_make_snapshot(
            polymarket_module,
            token_id="token-exit-int-005",
            best_bid=Decimal("0.69"),
            best_ask=Decimal("0.71"),
            midpoint=Decimal("0.70"),
        )
    )

    engine = engine_module.ExitStrategyEngine(
        config=_make_config(dry_run=False),
        polymarket_client=polymarket_client,
        db_session_factory=db_session_factory,
    )

    results = await engine.scan_open_positions()
    assert len(results) == 1
    assert results[0].should_exit is False

    async with db_session_factory() as verify_session:
        verify_repo = repo_module.PositionRepository(verify_session)
        refreshed = await verify_repo.get_by_id("pos-exit-int-005")
    assert refreshed is not None
    assert refreshed.status == "OPEN"


@pytest.mark.asyncio
async def test_full_flow_stop_loss_transitions_position_to_closed(
    async_session,
    db_session_factory,
):
    (
        engine_module,
        schema_module,
        models_module,
        repo_module,
        polymarket_module,
    ) = _load_contracts()

    repo = repo_module.PositionRepository(async_session)
    position = _make_position_orm(
        models_module,
        position_id="pos-exit-int-006",
        condition_id="condition-exit-int-006",
        token_id="token-exit-int-006",
        entry_price=Decimal("0.65"),
        routed_at_utc=datetime.now(timezone.utc) - timedelta(hours=2),
    )
    await repo.insert_position(position)
    await async_session.commit()

    polymarket_client = MagicMock()
    polymarket_client.fetch_order_book = AsyncMock(
        return_value=_make_snapshot(
            polymarket_module,
            token_id="token-exit-int-006",
            best_bid=Decimal("0.39"),
            best_ask=Decimal("0.41"),
            midpoint=Decimal("0.40"),
        )
    )

    engine = engine_module.ExitStrategyEngine(
        config=_make_config(dry_run=False),
        polymarket_client=polymarket_client,
        db_session_factory=db_session_factory,
    )

    results = await engine.scan_open_positions()
    assert len(results) == 1
    assert results[0].should_exit is True
    assert results[0].exit_reason == schema_module.ExitReason.STOP_LOSS

    async with db_session_factory() as verify_session:
        verify_repo = repo_module.PositionRepository(verify_session)
        refreshed = await verify_repo.get_by_id("pos-exit-int-006")
    assert refreshed is not None
    assert refreshed.status == "CLOSED"


@pytest.mark.asyncio
async def test_full_flow_time_decay_transitions_position_to_closed(
    async_session,
    db_session_factory,
):
    (
        engine_module,
        schema_module,
        models_module,
        repo_module,
        polymarket_module,
    ) = _load_contracts()

    repo = repo_module.PositionRepository(async_session)
    position = _make_position_orm(
        models_module,
        position_id="pos-exit-int-007",
        condition_id="condition-exit-int-007",
        token_id="token-exit-int-007",
        entry_price=Decimal("0.65"),
        routed_at_utc=datetime.now(timezone.utc) - timedelta(hours=72),
    )
    await repo.insert_position(position)
    await async_session.commit()

    polymarket_client = MagicMock()
    polymarket_client.fetch_order_book = AsyncMock(
        return_value=_make_snapshot(
            polymarket_module,
            token_id="token-exit-int-007",
            best_bid=Decimal("0.69"),
            best_ask=Decimal("0.71"),
            midpoint=Decimal("0.70"),
        )
    )

    engine = engine_module.ExitStrategyEngine(
        config=_make_config(dry_run=False),
        polymarket_client=polymarket_client,
        db_session_factory=db_session_factory,
    )

    results = await engine.scan_open_positions()
    assert len(results) == 1
    assert results[0].should_exit is True
    assert results[0].exit_reason == schema_module.ExitReason.TIME_DECAY

    async with db_session_factory() as verify_session:
        verify_repo = repo_module.PositionRepository(verify_session)
        refreshed = await verify_repo.get_by_id("pos-exit-int-007")
    assert refreshed is not None
    assert refreshed.status == "CLOSED"


def test_exit_strategy_engine_module_import_boundary():
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
        module_name for module_name in imported if module_name.startswith(forbidden_prefixes)
    )
    assert forbidden == []
