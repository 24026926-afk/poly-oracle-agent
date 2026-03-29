"""
tests/integration/test_position_tracker_integration.py

RED-phase integration tests for WI-17 Position Tracker and PositionRepository.
"""

from __future__ import annotations

import ast
import importlib
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest


SCHEMA_MODULE_NAME = "src.schemas.execution"
MODELS_MODULE_NAME = "src.db.models"
REPO_MODULE_NAME = "src.db.repositories.position_repo"
TRACKER_MODULE_NAME = "src.agents.execution.position_tracker"
TRACKER_MODULE_PATH = Path("src/agents/execution/position_tracker.py")


def _load_module(name: str):
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError:
        pytest.fail(f"Expected module {name} to exist.", pytrace=False)
    except Exception as exc:
        pytest.fail(f"Module {name} import failed unexpectedly: {exc!r}", pytrace=False)


def _sample_position_orm(models_module):
    position_cls = getattr(models_module, "Position", None)
    assert position_cls is not None, "Expected Position ORM model in src.db.models"

    return position_cls(
        id="pos-int-001",
        condition_id="condition-int-1",
        token_id="token-int-1",
        status="OPEN",
        side="BUY",
        entry_price=Decimal("0.55"),
        order_size_usdc=Decimal("10"),
        kelly_fraction=Decimal("0.10"),
        best_ask_at_entry=Decimal("0.56"),
        bankroll_usdc_at_entry=Decimal("1000"),
        execution_action="EXECUTED",
        reason=None,
        routed_at_utc=datetime.now(timezone.utc),
        recorded_at_utc=datetime.now(timezone.utc),
    )


def _build_execution_result(schema_module, *, action: str):
    result_cls = getattr(schema_module, "ExecutionResult", None)
    action_cls = getattr(schema_module, "ExecutionAction", None)
    assert result_cls is not None
    assert action_cls is not None

    return result_cls(
        action=getattr(action_cls, action),
        reason="integration_test",
        order_payload=None,
        signed_order=None,
        kelly_fraction=Decimal("0.12"),
        order_size_usdc=Decimal("12"),
        midpoint_probability=Decimal("0.55"),
        best_ask=Decimal("0.56"),
        bankroll_usdc=Decimal("100"),
        routed_at_utc=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_position_repository_insert_round_trip(async_session):
    models_module = _load_module(MODELS_MODULE_NAME)
    repo_module = _load_module(REPO_MODULE_NAME)

    repo_cls = getattr(repo_module, "PositionRepository", None)
    assert repo_cls is not None, "Expected PositionRepository in src.db.repositories.position_repo"

    repo = repo_cls(async_session)
    position = _sample_position_orm(models_module)

    inserted = await repo.insert_position(position)
    fetched = await repo.get_by_id(inserted.id)

    assert fetched is not None
    assert fetched.condition_id == "condition-int-1"
    assert Decimal(str(fetched.entry_price)) == Decimal("0.55")


@pytest.mark.asyncio
async def test_position_repository_get_open_by_condition_id(async_session):
    models_module = _load_module(MODELS_MODULE_NAME)
    repo_module = _load_module(REPO_MODULE_NAME)

    repo = repo_module.PositionRepository(async_session)

    open_match = _sample_position_orm(models_module)
    open_other = _sample_position_orm(models_module)
    open_other.id = "pos-int-002"
    open_other.condition_id = "condition-int-2"
    closed_match = _sample_position_orm(models_module)
    closed_match.id = "pos-int-003"
    closed_match.status = "CLOSED"

    await repo.insert_position(open_match)
    await repo.insert_position(open_other)
    await repo.insert_position(closed_match)

    rows = await repo.get_open_by_condition_id("condition-int-1")

    assert len(rows) == 1
    assert rows[0].id == "pos-int-001"


@pytest.mark.asyncio
async def test_position_repository_get_open_positions(async_session):
    models_module = _load_module(MODELS_MODULE_NAME)
    repo_module = _load_module(REPO_MODULE_NAME)

    repo = repo_module.PositionRepository(async_session)

    open_a = _sample_position_orm(models_module)
    open_b = _sample_position_orm(models_module)
    open_b.id = "pos-int-004"
    open_b.condition_id = "condition-int-3"
    failed = _sample_position_orm(models_module)
    failed.id = "pos-int-005"
    failed.status = "FAILED"

    await repo.insert_position(open_a)
    await repo.insert_position(open_b)
    await repo.insert_position(failed)

    rows = await repo.get_open_positions()

    ids = {row.id for row in rows}
    assert ids == {"pos-int-001", "pos-int-004"}


@pytest.mark.asyncio
async def test_position_repository_update_status_open_to_closed(async_session):
    models_module = _load_module(MODELS_MODULE_NAME)
    repo_module = _load_module(REPO_MODULE_NAME)

    repo = repo_module.PositionRepository(async_session)
    position = _sample_position_orm(models_module)
    await repo.insert_position(position)

    updated = await repo.update_status(position.id, new_status="CLOSED")

    assert updated is not None
    assert updated.status == "CLOSED"


@pytest.mark.asyncio
async def test_position_tracker_full_flow_executed(async_session, db_session_factory):
    schema_module = _load_module(SCHEMA_MODULE_NAME)
    tracker_module = _load_module(TRACKER_MODULE_NAME)
    repo_module = _load_module(REPO_MODULE_NAME)

    config = type("Cfg", (), {"dry_run": False})()
    tracker = tracker_module.PositionTracker(
        config=config,
        db_session_factory=db_session_factory,
    )

    result = _build_execution_result(schema_module, action="EXECUTED")
    record = await tracker.record_execution(
        result=result,
        condition_id="condition-int-1",
        token_id="token-int-1",
    )

    assert record is not None
    repo = repo_module.PositionRepository(async_session)
    open_rows = await repo.get_open_positions()
    assert any(row.id == record.id for row in open_rows)


@pytest.mark.asyncio
async def test_position_tracker_full_flow_failed(async_session, db_session_factory):
    schema_module = _load_module(SCHEMA_MODULE_NAME)
    tracker_module = _load_module(TRACKER_MODULE_NAME)
    repo_module = _load_module(REPO_MODULE_NAME)

    config = type("Cfg", (), {"dry_run": False})()
    tracker = tracker_module.PositionTracker(
        config=config,
        db_session_factory=db_session_factory,
    )

    result = _build_execution_result(schema_module, action="FAILED")
    record = await tracker.record_execution(
        result=result,
        condition_id="condition-int-1",
        token_id="token-int-1",
    )

    assert record is not None
    repo = repo_module.PositionRepository(async_session)
    fetched = await repo.get_by_id(record.id)
    assert fetched is not None
    assert fetched.status == "FAILED"


def test_position_tracker_module_import_boundary():
    if not TRACKER_MODULE_PATH.exists():
        pytest.fail(
            "Expected position tracker implementation file at "
            "src/agents/execution/position_tracker.py.",
            pytrace=False,
        )

    tree = ast.parse(TRACKER_MODULE_PATH.read_text())
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
