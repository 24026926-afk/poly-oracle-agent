"""
tests/unit/test_position_tracker.py

RED-phase tests for WI-17 Position Tracker.

These tests intentionally codify the WI-17 contracts before
production implementation exists.
"""

from __future__ import annotations

import ast
import importlib
import inspect
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


SCHEMA_MODULE_NAME = "src.schemas.execution"
TRACKER_MODULE_NAME = "src.agents.execution.position_tracker"
TRACKER_MODULE_PATH = Path("src/agents/execution/position_tracker.py")
FORBIDDEN_IMPORT_PREFIXES = (
    "src.agents.context",
    "src.agents.evaluation",
    "src.agents.ingestion",
)


def _load_schema_module():
    try:
        return importlib.import_module(SCHEMA_MODULE_NAME)
    except ModuleNotFoundError:
        pytest.fail(
            "Expected WI-17 schema module src.schemas.execution to exist.",
            pytrace=False,
        )
    except Exception as exc:
        pytest.fail(
            f"Execution schema module import failed unexpectedly: {exc!r}",
            pytrace=False,
        )


def _load_tracker_module():
    try:
        return importlib.import_module(TRACKER_MODULE_NAME)
    except ModuleNotFoundError:
        pytest.fail(
            "Expected WI-17 module src.agents.execution.position_tracker to exist.",
            pytrace=False,
        )
    except Exception as exc:
        pytest.fail(
            f"Position tracker module import failed unexpectedly: {exc!r}",
            pytrace=False,
        )


def _build_execution_result(schema_module, *, action: str):
    action_enum = schema_module.ExecutionAction
    result_cls = schema_module.ExecutionResult
    return result_cls(
        action=getattr(action_enum, action),
        reason="test_reason",
        order_payload=None,
        signed_order=None,
        kelly_fraction=Decimal("0.12"),
        order_size_usdc=Decimal("12"),
        midpoint_probability=Decimal("0.55"),
        best_ask=Decimal("0.56"),
        bankroll_usdc=Decimal("100"),
        routed_at_utc=datetime.now(timezone.utc),
    )


def _build_failed_result_with_none_financials(schema_module):
    action_enum = schema_module.ExecutionAction
    result_cls = schema_module.ExecutionResult
    return result_cls(
        action=action_enum.FAILED,
        reason="failed_before_sizing",
        order_payload=None,
        signed_order=None,
        kelly_fraction=None,
        order_size_usdc=None,
        midpoint_probability=None,
        best_ask=None,
        bankroll_usdc=None,
        routed_at_utc=datetime.now(timezone.utc),
    )


def _build_tracker_config(*, dry_run: bool):
    return SimpleNamespace(dry_run=dry_run)


def test_position_status_enum_exists_with_expected_values():
    schema_module = _load_schema_module()

    status_cls = getattr(schema_module, "PositionStatus", None)
    assert status_cls is not None, "Expected PositionStatus enum in src.schemas.execution"
    assert {member.value for member in status_cls} == {"OPEN", "CLOSED", "FAILED"}


@pytest.mark.parametrize(
    "field_name",
    [
        "entry_price",
        "order_size_usdc",
        "kelly_fraction",
        "best_ask_at_entry",
        "bankroll_usdc_at_entry",
    ],
)
def test_position_record_rejects_float_financial_fields(field_name):
    schema_module = _load_schema_module()

    model_cls = getattr(schema_module, "PositionRecord", None)
    status_cls = getattr(schema_module, "PositionStatus", None)
    action_cls = getattr(schema_module, "ExecutionAction", None)

    assert model_cls is not None, "Expected PositionRecord model in src.schemas.execution"
    assert status_cls is not None
    assert action_cls is not None

    payload = {
        "id": "pos-test-001",
        "condition_id": "condition-123",
        "token_id": "token-yes-123",
        "status": status_cls.OPEN,
        "side": "BUY",
        "entry_price": Decimal("0.55"),
        "order_size_usdc": Decimal("10"),
        "kelly_fraction": Decimal("0.10"),
        "best_ask_at_entry": Decimal("0.56"),
        "bankroll_usdc_at_entry": Decimal("1000"),
        "execution_action": action_cls.EXECUTED,
        "reason": None,
        "routed_at_utc": datetime.now(timezone.utc),
        "recorded_at_utc": datetime.now(timezone.utc),
    }
    payload[field_name] = 1.23

    with pytest.raises(Exception):
        model_cls(**payload)


def test_position_record_accepts_decimal_financial_fields_and_is_frozen():
    schema_module = _load_schema_module()

    model_cls = getattr(schema_module, "PositionRecord", None)
    status_cls = getattr(schema_module, "PositionStatus", None)
    action_cls = getattr(schema_module, "ExecutionAction", None)

    assert model_cls is not None, "Expected PositionRecord model in src.schemas.execution"

    record = model_cls(
        id="pos-test-002",
        condition_id="condition-123",
        token_id="token-yes-123",
        status=status_cls.OPEN,
        side="BUY",
        entry_price=Decimal("0.55"),
        order_size_usdc=Decimal("10"),
        kelly_fraction=Decimal("0.10"),
        best_ask_at_entry=Decimal("0.56"),
        bankroll_usdc_at_entry=Decimal("1000"),
        execution_action=action_cls.DRY_RUN,
        reason=None,
        routed_at_utc=datetime.now(timezone.utc),
        recorded_at_utc=datetime.now(timezone.utc),
    )

    assert isinstance(record.entry_price, Decimal)
    with pytest.raises(Exception):
        record.side = "SELL"


def test_position_tracker_contract_exists_and_has_one_public_method():
    module = _load_tracker_module()

    tracker_cls = getattr(module, "PositionTracker", None)
    assert tracker_cls is not None, "Expected PositionTracker class."
    assert inspect.isclass(tracker_cls)
    assert inspect.iscoroutinefunction(tracker_cls.record_execution)

    signature = inspect.signature(tracker_cls.record_execution)
    assert list(signature.parameters.keys()) == [
        "self",
        "result",
        "condition_id",
        "token_id",
    ]

    public_methods = [
        name
        for name, member in inspect.getmembers(tracker_cls, predicate=inspect.isfunction)
        if not name.startswith("_")
    ]
    assert public_methods == ["record_execution"]


@pytest.mark.asyncio
async def test_status_mapping_executed_to_open():
    schema_module = _load_schema_module()
    tracker_module = _load_tracker_module()

    tracker = tracker_module.PositionTracker(
        config=_build_tracker_config(dry_run=False),
        db_session_factory=SimpleNamespace(),
    )
    result = _build_execution_result(schema_module, action="EXECUTED")

    record = await tracker.record_execution(
        result=result,
        condition_id="condition-123",
        token_id="token-yes-123",
    )

    assert record is not None
    assert record.status == schema_module.PositionStatus.OPEN


@pytest.mark.asyncio
async def test_status_mapping_dry_run_to_open():
    schema_module = _load_schema_module()
    tracker_module = _load_tracker_module()

    tracker = tracker_module.PositionTracker(
        config=_build_tracker_config(dry_run=True),
        db_session_factory=SimpleNamespace(),
    )
    result = _build_execution_result(schema_module, action="DRY_RUN")

    record = await tracker.record_execution(
        result=result,
        condition_id="condition-123",
        token_id="token-yes-123",
    )

    assert record is not None
    assert record.status == schema_module.PositionStatus.OPEN


@pytest.mark.asyncio
async def test_status_mapping_failed_to_failed():
    schema_module = _load_schema_module()
    tracker_module = _load_tracker_module()

    tracker = tracker_module.PositionTracker(
        config=_build_tracker_config(dry_run=False),
        db_session_factory=SimpleNamespace(),
    )
    result = _build_execution_result(schema_module, action="FAILED")

    record = await tracker.record_execution(
        result=result,
        condition_id="condition-123",
        token_id="token-yes-123",
    )

    assert record is not None
    assert record.status == schema_module.PositionStatus.FAILED


@pytest.mark.asyncio
async def test_skip_returns_none_no_side_effects():
    schema_module = _load_schema_module()
    tracker_module = _load_tracker_module()

    tracker = tracker_module.PositionTracker(
        config=_build_tracker_config(dry_run=False),
        db_session_factory=SimpleNamespace(),
    )
    result = _build_execution_result(schema_module, action="SKIP")

    record = await tracker.record_execution(
        result=result,
        condition_id="condition-123",
        token_id="token-yes-123",
    )

    assert record is None


@pytest.mark.asyncio
async def test_failed_result_none_financials_uses_decimal_zero_sentinels():
    schema_module = _load_schema_module()
    tracker_module = _load_tracker_module()

    tracker = tracker_module.PositionTracker(
        config=_build_tracker_config(dry_run=False),
        db_session_factory=SimpleNamespace(),
    )
    result = _build_failed_result_with_none_financials(schema_module)

    record = await tracker.record_execution(
        result=result,
        condition_id="condition-123",
        token_id="token-yes-123",
    )

    assert record is not None
    assert record.entry_price == Decimal("0")
    assert record.order_size_usdc == Decimal("0")
    assert record.kelly_fraction == Decimal("0")
    assert record.best_ask_at_entry == Decimal("0")
    assert record.bankroll_usdc_at_entry == Decimal("0")


@pytest.mark.asyncio
async def test_dry_run_true_does_not_open_session_or_instantiate_repository(monkeypatch):
    schema_module = _load_schema_module()
    tracker_module = _load_tracker_module()

    session_factory = MagicMock()
    tracker = tracker_module.PositionTracker(
        config=_build_tracker_config(dry_run=True),
        db_session_factory=session_factory,
    )
    result = _build_execution_result(schema_module, action="DRY_RUN")

    await tracker.record_execution(
        result=result,
        condition_id="condition-123",
        token_id="token-yes-123",
    )

    session_factory.assert_not_called()


@pytest.mark.asyncio
async def test_dry_run_true_emits_structured_log(monkeypatch):
    schema_module = _load_schema_module()
    tracker_module = _load_tracker_module()

    logger = MagicMock()
    monkeypatch.setattr(tracker_module, "logger", logger)
    tracker = tracker_module.PositionTracker(
        config=_build_tracker_config(dry_run=True),
        db_session_factory=MagicMock(),
    )
    result = _build_execution_result(schema_module, action="DRY_RUN")

    record = await tracker.record_execution(
        result=result,
        condition_id="condition-123",
        token_id="token-yes-123",
    )

    assert record is not None
    logger.info.assert_called()
    event_name = logger.info.call_args.args[0]
    assert event_name == "position_tracker.dry_run_record"
    kwargs = logger.info.call_args.kwargs
    assert kwargs["condition_id"] == "condition-123"
    assert kwargs["token_id"] == "token-yes-123"


@pytest.mark.asyncio
async def test_live_executed_calls_insert_position(monkeypatch):
    schema_module = _load_schema_module()
    tracker_module = _load_tracker_module()

    class _SessionCtx:
        async def __aenter__(self):
            return MagicMock(commit=AsyncMock())

        async def __aexit__(self, exc_type, exc, tb):
            return False

    session_factory = MagicMock(return_value=_SessionCtx())
    tracker = tracker_module.PositionTracker(
        config=_build_tracker_config(dry_run=False),
        db_session_factory=session_factory,
    )
    result = _build_execution_result(schema_module, action="EXECUTED")

    repo_mock = MagicMock()
    repo_mock.insert_position = AsyncMock()
    monkeypatch.setattr(tracker_module, "PositionRepository", MagicMock(return_value=repo_mock))

    await tracker.record_execution(
        result=result,
        condition_id="condition-123",
        token_id="token-yes-123",
    )

    repo_mock.insert_position.assert_awaited_once()


@pytest.mark.asyncio
async def test_live_failed_calls_insert_position(monkeypatch):
    schema_module = _load_schema_module()
    tracker_module = _load_tracker_module()

    class _SessionCtx:
        async def __aenter__(self):
            return MagicMock(commit=AsyncMock())

        async def __aexit__(self, exc_type, exc, tb):
            return False

    session_factory = MagicMock(return_value=_SessionCtx())
    tracker = tracker_module.PositionTracker(
        config=_build_tracker_config(dry_run=False),
        db_session_factory=session_factory,
    )
    result = _build_execution_result(schema_module, action="FAILED")

    repo_mock = MagicMock()
    repo_mock.insert_position = AsyncMock()
    monkeypatch.setattr(tracker_module, "PositionRepository", MagicMock(return_value=repo_mock))

    await tracker.record_execution(
        result=result,
        condition_id="condition-123",
        token_id="token-yes-123",
    )

    repo_mock.insert_position.assert_awaited_once()


@pytest.mark.asyncio
async def test_unreachable_executed_in_dry_run_logs_error_and_returns_none(monkeypatch):
    schema_module = _load_schema_module()
    tracker_module = _load_tracker_module()

    logger = MagicMock()
    monkeypatch.setattr(tracker_module, "logger", logger)
    tracker = tracker_module.PositionTracker(
        config=_build_tracker_config(dry_run=True),
        db_session_factory=MagicMock(),
    )
    result = _build_execution_result(schema_module, action="EXECUTED")

    record = await tracker.record_execution(
        result=result,
        condition_id="condition-123",
        token_id="token-yes-123",
    )

    assert record is None
    logger.error.assert_called_once()
    assert logger.error.call_args.args[0] == "position_tracker.unreachable_executed_in_dry_run"


@pytest.mark.asyncio
async def test_unreachable_dry_run_in_live_logs_error_and_returns_none(monkeypatch):
    schema_module = _load_schema_module()
    tracker_module = _load_tracker_module()

    logger = MagicMock()
    monkeypatch.setattr(tracker_module, "logger", logger)
    tracker = tracker_module.PositionTracker(
        config=_build_tracker_config(dry_run=False),
        db_session_factory=MagicMock(),
    )
    result = _build_execution_result(schema_module, action="DRY_RUN")

    record = await tracker.record_execution(
        result=result,
        condition_id="condition-123",
        token_id="token-yes-123",
    )

    assert record is None
    logger.error.assert_called_once()
    assert logger.error.call_args.args[0] == "position_tracker.unreachable_dry_run_in_live"


def test_position_tracker_module_has_no_forbidden_imports():
    if not TRACKER_MODULE_PATH.exists():
        pytest.fail(
            "Expected position tracker implementation file at "
            "src/agents/execution/position_tracker.py.",
            pytrace=False,
        )

    tree = ast.parse(TRACKER_MODULE_PATH.read_text())
    imported_modules: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported_modules.add(node.module)

    forbidden = sorted(
        module_name
        for module_name in imported_modules
        if module_name.startswith(FORBIDDEN_IMPORT_PREFIXES)
    )
    assert forbidden == []
