"""
tests/unit/test_exit_strategy_engine.py

RED-phase unit tests for WI-19 Exit Strategy Engine.

These tests codify the WI-19 contracts before production implementation
changes are made.
"""

from __future__ import annotations

import ast
import importlib
import inspect
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


SCHEMA_MODULE_NAME = "src.schemas.execution"
ENGINE_MODULE_NAME = "src.agents.execution.exit_strategy_engine"
ENGINE_MODULE_PATH = Path("src/agents/execution/exit_strategy_engine.py")
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
            "Expected WI-19 schema module src.schemas.execution to exist.",
            pytrace=False,
        )
    except Exception as exc:
        pytest.fail(
            f"Execution schema module import failed unexpectedly: {exc!r}",
            pytrace=False,
        )


def _load_engine_module():
    try:
        return importlib.import_module(ENGINE_MODULE_NAME)
    except ModuleNotFoundError:
        pytest.fail(
            "Expected WI-19 module src.agents.execution.exit_strategy_engine to exist.",
            pytrace=False,
        )
    except Exception as exc:
        pytest.fail(
            f"Exit strategy engine module import failed unexpectedly: {exc!r}",
            pytrace=False,
        )


def _build_config(*, dry_run: bool):
    return SimpleNamespace(
        dry_run=dry_run,
        exit_position_max_age_hours=Decimal("48"),
        exit_stop_loss_drop=Decimal("0.15"),
        exit_take_profit_gain=Decimal("0.20"),
    )


def _build_position_record(
    schema_module,
    *,
    status: str = "OPEN",
    entry_price: Decimal = Decimal("0.65"),
    routed_at_utc: datetime | None = None,
):
    if routed_at_utc is None:
        routed_at_utc = datetime.now(timezone.utc) - timedelta(hours=1)

    return schema_module.PositionRecord(
        id="pos-unit-001",
        condition_id="condition-unit-001",
        token_id="token-unit-001",
        status=getattr(schema_module.PositionStatus, status),
        side="BUY",
        entry_price=entry_price,
        order_size_usdc=Decimal("10"),
        kelly_fraction=Decimal("0.10"),
        best_ask_at_entry=entry_price + Decimal("0.01"),
        bankroll_usdc_at_entry=Decimal("1000"),
        execution_action=schema_module.ExecutionAction.EXECUTED,
        reason=None,
        routed_at_utc=routed_at_utc,
        recorded_at_utc=routed_at_utc,
    )


def _build_exit_signal(
    schema_module,
    *,
    position=None,
    current_midpoint: Decimal = Decimal("0.70"),
    current_best_bid: Decimal = Decimal("0.69"),
    evaluated_at_utc: datetime | None = None,
):
    signal_cls = getattr(schema_module, "ExitSignal", None)
    assert signal_cls is not None, "Expected ExitSignal model."

    if position is None:
        position = _build_position_record(schema_module)
    if evaluated_at_utc is None:
        evaluated_at_utc = datetime.now(timezone.utc)

    return signal_cls(
        position=position,
        current_midpoint=current_midpoint,
        current_best_bid=current_best_bid,
        evaluated_at_utc=evaluated_at_utc,
    )


def _build_exit_result(schema_module, *, overrides: dict | None = None):
    result_cls = getattr(schema_module, "ExitResult", None)
    reason_cls = getattr(schema_module, "ExitReason", None)
    assert result_cls is not None, "Expected ExitResult model."
    assert reason_cls is not None, "Expected ExitReason enum."

    payload = {
        "position_id": "pos-unit-001",
        "condition_id": "condition-unit-001",
        "should_exit": True,
        "exit_reason": reason_cls.STOP_LOSS,
        "entry_price": Decimal("0.65"),
        "current_midpoint": Decimal("0.45"),
        "current_best_bid": Decimal("0.44"),
        "position_age_hours": Decimal("12"),
        "unrealized_edge": Decimal("-0.20"),
        "evaluated_at_utc": datetime.now(timezone.utc),
    }
    if overrides:
        payload.update(overrides)
    return result_cls(**payload)


def _build_engine(engine_module, *, dry_run: bool, session_factory=None):
    if session_factory is None:
        session_factory = MagicMock()
    polymarket_client = MagicMock()
    polymarket_client.fetch_order_book = AsyncMock()
    return engine_module.ExitStrategyEngine(
        config=_build_config(dry_run=dry_run),
        polymarket_client=polymarket_client,
        db_session_factory=session_factory,
    )


def test_exit_reason_enum_exists_with_expected_values():
    schema_module = _load_schema_module()

    reason_cls = getattr(schema_module, "ExitReason", None)
    assert reason_cls is not None, "Expected ExitReason enum in src.schemas.execution"
    assert {member.value for member in reason_cls} == {
        "NO_EDGE",
        "STOP_LOSS",
        "TIME_DECAY",
        "TAKE_PROFIT",
        "STALE_MARKET",
        "ERROR",
    }


def test_exit_signal_schema_exists_with_expected_fields_and_is_frozen():
    schema_module = _load_schema_module()

    signal_cls = getattr(schema_module, "ExitSignal", None)
    assert signal_cls is not None, "Expected ExitSignal model in src.schemas.execution"
    assert {
        "position",
        "current_midpoint",
        "current_best_bid",
        "evaluated_at_utc",
    }.issubset(signal_cls.model_fields.keys())
    assert signal_cls.model_fields["current_midpoint"].annotation is Decimal
    assert signal_cls.model_fields["current_best_bid"].annotation is Decimal

    signal = _build_exit_signal(schema_module)
    with pytest.raises(Exception):
        signal.current_midpoint = Decimal("0.50")


@pytest.mark.parametrize("field_name", ["current_midpoint", "current_best_bid"])
def test_exit_signal_rejects_float_financial_fields(field_name):
    schema_module = _load_schema_module()

    payload = {
        "position": _build_position_record(schema_module),
        "current_midpoint": Decimal("0.70"),
        "current_best_bid": Decimal("0.69"),
        "evaluated_at_utc": datetime.now(timezone.utc),
    }
    payload[field_name] = 0.71

    signal_cls = getattr(schema_module, "ExitSignal", None)
    assert signal_cls is not None, "Expected ExitSignal model in src.schemas.execution"
    with pytest.raises(Exception):
        signal_cls(**payload)


def test_exit_signal_accepts_decimal_financial_fields():
    schema_module = _load_schema_module()
    signal = _build_exit_signal(
        schema_module,
        current_midpoint=Decimal("0.72"),
        current_best_bid=Decimal("0.71"),
    )

    assert isinstance(signal.current_midpoint, Decimal)
    assert isinstance(signal.current_best_bid, Decimal)


def test_exit_result_schema_exists_with_expected_fields_and_is_frozen():
    schema_module = _load_schema_module()

    result_cls = getattr(schema_module, "ExitResult", None)
    assert result_cls is not None, "Expected ExitResult model in src.schemas.execution"
    assert {
        "position_id",
        "condition_id",
        "should_exit",
        "exit_reason",
        "entry_price",
        "current_midpoint",
        "current_best_bid",
        "position_age_hours",
        "unrealized_edge",
        "evaluated_at_utc",
    }.issubset(result_cls.model_fields.keys())
    assert result_cls.model_fields["entry_price"].annotation is Decimal
    assert result_cls.model_fields["current_midpoint"].annotation is Decimal
    assert result_cls.model_fields["current_best_bid"].annotation is Decimal
    assert result_cls.model_fields["position_age_hours"].annotation is Decimal
    assert result_cls.model_fields["unrealized_edge"].annotation is Decimal

    result = _build_exit_result(schema_module)
    with pytest.raises(Exception):
        result.position_id = "pos-unit-999"


@pytest.mark.parametrize(
    "field_name",
    [
        "entry_price",
        "current_midpoint",
        "current_best_bid",
        "position_age_hours",
        "unrealized_edge",
    ],
)
def test_exit_result_rejects_float_financial_fields(field_name):
    schema_module = _load_schema_module()
    overrides = {field_name: 0.123}

    with pytest.raises(Exception):
        _build_exit_result(schema_module, overrides=overrides)


def test_exit_result_accepts_decimal_financial_fields():
    schema_module = _load_schema_module()
    result = _build_exit_result(
        schema_module,
        overrides={
            "entry_price": Decimal("0.65"),
            "current_midpoint": Decimal("0.70"),
            "current_best_bid": Decimal("0.69"),
            "position_age_hours": Decimal("6"),
            "unrealized_edge": Decimal("0.05"),
        },
    )
    assert isinstance(result.entry_price, Decimal)
    assert isinstance(result.current_midpoint, Decimal)
    assert isinstance(result.current_best_bid, Decimal)
    assert isinstance(result.position_age_hours, Decimal)
    assert isinstance(result.unrealized_edge, Decimal)


def test_exit_strategy_engine_contract_exists_and_has_two_public_methods():
    module = _load_engine_module()
    engine_cls = getattr(module, "ExitStrategyEngine", None)

    assert engine_cls is not None, "Expected ExitStrategyEngine class."
    assert inspect.isclass(engine_cls)
    assert inspect.iscoroutinefunction(engine_cls.evaluate_position)
    assert inspect.iscoroutinefunction(engine_cls.scan_open_positions)

    public_methods = [
        name
        for name, member in inspect.getmembers(engine_cls, predicate=inspect.isfunction)
        if not name.startswith("_")
    ]
    assert public_methods == ["evaluate_position", "scan_open_positions"]


@pytest.mark.asyncio
async def test_status_gate_closed_position_returns_error_hold():
    schema_module = _load_schema_module()
    engine_module = _load_engine_module()

    position = _build_position_record(schema_module, status="CLOSED")
    signal = _build_exit_signal(schema_module, position=position)
    engine = _build_engine(engine_module, dry_run=True)

    result = await engine.evaluate_position(signal)

    assert result.should_exit is False
    assert result.exit_reason == schema_module.ExitReason.ERROR


@pytest.mark.asyncio
async def test_status_gate_failed_position_returns_error_hold():
    schema_module = _load_schema_module()
    engine_module = _load_engine_module()

    position = _build_position_record(schema_module, status="FAILED")
    signal = _build_exit_signal(schema_module, position=position)
    engine = _build_engine(engine_module, dry_run=True)

    result = await engine.evaluate_position(signal)

    assert result.should_exit is False
    assert result.exit_reason == schema_module.ExitReason.ERROR


@pytest.mark.asyncio
async def test_stop_loss_triggers_when_edge_breaches_threshold():
    schema_module = _load_schema_module()
    engine_module = _load_engine_module()

    position = _build_position_record(schema_module, entry_price=Decimal("0.65"))
    signal = _build_exit_signal(
        schema_module,
        position=position,
        current_midpoint=Decimal("0.45"),
        current_best_bid=Decimal("0.44"),
    )
    engine = _build_engine(engine_module, dry_run=True)

    result = await engine.evaluate_position(signal)
    assert result.should_exit is True
    assert result.exit_reason == schema_module.ExitReason.STOP_LOSS


@pytest.mark.asyncio
async def test_stop_loss_not_triggered_when_drop_is_smaller_than_threshold():
    schema_module = _load_schema_module()
    engine_module = _load_engine_module()

    position = _build_position_record(schema_module, entry_price=Decimal("0.65"))
    signal = _build_exit_signal(
        schema_module,
        position=position,
        current_midpoint=Decimal("0.60"),
        current_best_bid=Decimal("0.59"),
    )
    engine = _build_engine(engine_module, dry_run=True)

    result = await engine.evaluate_position(signal)
    assert result.exit_reason != schema_module.ExitReason.STOP_LOSS


@pytest.mark.asyncio
async def test_time_decay_triggers_when_age_meets_threshold():
    schema_module = _load_schema_module()
    engine_module = _load_engine_module()

    old_routed_at = datetime.now(timezone.utc) - timedelta(hours=72)
    position = _build_position_record(
        schema_module,
        entry_price=Decimal("0.65"),
        routed_at_utc=old_routed_at,
    )
    signal = _build_exit_signal(
        schema_module,
        position=position,
        current_midpoint=Decimal("0.70"),
        current_best_bid=Decimal("0.69"),
        evaluated_at_utc=datetime.now(timezone.utc),
    )
    engine = _build_engine(engine_module, dry_run=True)

    result = await engine.evaluate_position(signal)
    assert result.should_exit is True
    assert result.exit_reason == schema_module.ExitReason.TIME_DECAY


@pytest.mark.asyncio
async def test_time_decay_not_triggered_for_young_position_with_positive_edge():
    schema_module = _load_schema_module()
    engine_module = _load_engine_module()

    recent_routed_at = datetime.now(timezone.utc) - timedelta(hours=2)
    position = _build_position_record(
        schema_module,
        entry_price=Decimal("0.65"),
        routed_at_utc=recent_routed_at,
    )
    signal = _build_exit_signal(
        schema_module,
        position=position,
        current_midpoint=Decimal("0.70"),
        current_best_bid=Decimal("0.69"),
    )
    engine = _build_engine(engine_module, dry_run=True)

    result = await engine.evaluate_position(signal)
    assert result.should_exit is False


@pytest.mark.asyncio
async def test_no_edge_triggers_when_midpoint_not_above_entry():
    schema_module = _load_schema_module()
    engine_module = _load_engine_module()

    position = _build_position_record(schema_module, entry_price=Decimal("0.65"))
    signal = _build_exit_signal(
        schema_module,
        position=position,
        current_midpoint=Decimal("0.65"),
        current_best_bid=Decimal("0.64"),
    )
    engine = _build_engine(engine_module, dry_run=True)

    result = await engine.evaluate_position(signal)
    assert result.should_exit is True
    assert result.exit_reason == schema_module.ExitReason.NO_EDGE


@pytest.mark.asyncio
async def test_take_profit_triggers_when_gain_meets_threshold():
    schema_module = _load_schema_module()
    engine_module = _load_engine_module()

    position = _build_position_record(schema_module, entry_price=Decimal("0.65"))
    signal = _build_exit_signal(
        schema_module,
        position=position,
        current_midpoint=Decimal("0.90"),
        current_best_bid=Decimal("0.89"),
    )
    engine = _build_engine(engine_module, dry_run=True)

    result = await engine.evaluate_position(signal)
    assert result.should_exit is True
    assert result.exit_reason == schema_module.ExitReason.TAKE_PROFIT


@pytest.mark.asyncio
async def test_conservative_hold_when_no_exit_criterion_triggers():
    schema_module = _load_schema_module()
    engine_module = _load_engine_module()

    position = _build_position_record(schema_module, entry_price=Decimal("0.65"))
    signal = _build_exit_signal(
        schema_module,
        position=position,
        current_midpoint=Decimal("0.70"),
        current_best_bid=Decimal("0.69"),
    )
    engine = _build_engine(engine_module, dry_run=True)

    result = await engine.evaluate_position(signal)
    assert result.should_exit is False


@pytest.mark.asyncio
async def test_priority_stop_loss_over_time_decay():
    schema_module = _load_schema_module()
    engine_module = _load_engine_module()

    old_routed_at = datetime.now(timezone.utc) - timedelta(hours=72)
    position = _build_position_record(
        schema_module,
        entry_price=Decimal("0.65"),
        routed_at_utc=old_routed_at,
    )
    signal = _build_exit_signal(
        schema_module,
        position=position,
        current_midpoint=Decimal("0.40"),
        current_best_bid=Decimal("0.39"),
    )
    engine = _build_engine(engine_module, dry_run=True)

    result = await engine.evaluate_position(signal)
    assert result.should_exit is True
    assert result.exit_reason == schema_module.ExitReason.STOP_LOSS


@pytest.mark.asyncio
async def test_priority_time_decay_over_no_edge_when_no_stop_loss():
    schema_module = _load_schema_module()
    engine_module = _load_engine_module()

    old_routed_at = datetime.now(timezone.utc) - timedelta(hours=72)
    position = _build_position_record(
        schema_module,
        entry_price=Decimal("0.65"),
        routed_at_utc=old_routed_at,
    )
    signal = _build_exit_signal(
        schema_module,
        position=position,
        current_midpoint=Decimal("0.60"),
        current_best_bid=Decimal("0.59"),
    )
    engine = _build_engine(engine_module, dry_run=True)

    result = await engine.evaluate_position(signal)
    assert result.should_exit is True
    assert result.exit_reason == schema_module.ExitReason.TIME_DECAY


@pytest.mark.asyncio
async def test_position_age_calculation_returns_decimal_hours():
    schema_module = _load_schema_module()
    engine_module = _load_engine_module()

    evaluated_at_utc = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    routed_at_utc = evaluated_at_utc - timedelta(hours=5, minutes=30)
    position = _build_position_record(
        schema_module,
        entry_price=Decimal("0.65"),
        routed_at_utc=routed_at_utc,
    )
    signal = _build_exit_signal(
        schema_module,
        position=position,
        current_midpoint=Decimal("0.70"),
        current_best_bid=Decimal("0.69"),
        evaluated_at_utc=evaluated_at_utc,
    )
    engine = _build_engine(engine_module, dry_run=True)

    result = await engine.evaluate_position(signal)
    assert isinstance(result.position_age_hours, Decimal)
    assert result.position_age_hours == Decimal("5.5")


@pytest.mark.asyncio
async def test_unrealized_edge_is_computed_for_profitable_and_underwater_positions():
    schema_module = _load_schema_module()
    engine_module = _load_engine_module()
    engine = _build_engine(engine_module, dry_run=True)

    base_position = _build_position_record(schema_module, entry_price=Decimal("0.65"))
    profitable = _build_exit_signal(
        schema_module,
        position=base_position,
        current_midpoint=Decimal("0.75"),
        current_best_bid=Decimal("0.74"),
    )
    underwater = _build_exit_signal(
        schema_module,
        position=base_position,
        current_midpoint=Decimal("0.55"),
        current_best_bid=Decimal("0.54"),
    )

    profitable_result = await engine.evaluate_position(profitable)
    underwater_result = await engine.evaluate_position(underwater)

    assert profitable_result.unrealized_edge == Decimal("0.10")
    assert underwater_result.unrealized_edge == Decimal("-0.10")


@pytest.mark.asyncio
async def test_dry_run_true_does_not_open_session_or_mutate_position(monkeypatch):
    schema_module = _load_schema_module()
    engine_module = _load_engine_module()

    session_factory = MagicMock()
    repo_mock = MagicMock()
    repo_mock.update_status = AsyncMock()
    monkeypatch.setattr(
        engine_module,
        "PositionRepository",
        MagicMock(return_value=repo_mock),
    )

    engine = _build_engine(
        engine_module,
        dry_run=True,
        session_factory=session_factory,
    )
    position = _build_position_record(schema_module, entry_price=Decimal("0.65"))
    signal = _build_exit_signal(
        schema_module,
        position=position,
        current_midpoint=Decimal("0.45"),
        current_best_bid=Decimal("0.44"),
    )

    result = await engine.evaluate_position(signal)

    assert result.should_exit is True
    session_factory.assert_not_called()
    repo_mock.update_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_dry_run_true_emits_structured_log(monkeypatch):
    schema_module = _load_schema_module()
    engine_module = _load_engine_module()

    logger = MagicMock()
    monkeypatch.setattr(engine_module, "logger", logger)

    engine = _build_engine(engine_module, dry_run=True)
    signal = _build_exit_signal(
        schema_module,
        current_midpoint=Decimal("0.45"),
        current_best_bid=Decimal("0.44"),
    )

    await engine.evaluate_position(signal)

    info_events = [call.args[0] for call in logger.info.call_args_list if call.args]
    assert "exit_engine.dry_run_exit" in info_events


@pytest.mark.asyncio
async def test_live_should_exit_true_calls_update_status_closed(monkeypatch):
    schema_module = _load_schema_module()
    engine_module = _load_engine_module()

    class _SessionCtx:
        async def __aenter__(self):
            return MagicMock(commit=AsyncMock())

        async def __aexit__(self, exc_type, exc, tb):
            return False

    session_factory = MagicMock(return_value=_SessionCtx())
    repo_mock = MagicMock()
    repo_mock.update_status = AsyncMock(return_value=object())
    monkeypatch.setattr(
        engine_module,
        "PositionRepository",
        MagicMock(return_value=repo_mock),
    )

    engine = _build_engine(
        engine_module,
        dry_run=False,
        session_factory=session_factory,
    )
    signal = _build_exit_signal(
        schema_module,
        current_midpoint=Decimal("0.45"),
        current_best_bid=Decimal("0.44"),
    )

    result = await engine.evaluate_position(signal)

    assert result.should_exit is True
    session_factory.assert_called_once()
    repo_mock.update_status.assert_awaited_once()

    args = repo_mock.update_status.await_args.args
    kwargs = repo_mock.update_status.await_args.kwargs
    position_id = kwargs.get("position_id", args[0] if args else None)
    new_status = kwargs.get("new_status", args[1] if len(args) > 1 else None)
    assert position_id == signal.position.id
    assert new_status in {
        schema_module.PositionStatus.CLOSED,
        schema_module.PositionStatus.CLOSED.value,
    }


@pytest.mark.asyncio
async def test_live_should_exit_false_does_not_call_update_status(monkeypatch):
    schema_module = _load_schema_module()
    engine_module = _load_engine_module()

    session_factory = MagicMock()
    repo_mock = MagicMock()
    repo_mock.update_status = AsyncMock(return_value=object())
    monkeypatch.setattr(
        engine_module,
        "PositionRepository",
        MagicMock(return_value=repo_mock),
    )

    engine = _build_engine(
        engine_module,
        dry_run=False,
        session_factory=session_factory,
    )
    signal = _build_exit_signal(
        schema_module,
        current_midpoint=Decimal("0.70"),
        current_best_bid=Decimal("0.69"),
    )

    result = await engine.evaluate_position(signal)

    assert result.should_exit is False
    session_factory.assert_not_called()
    repo_mock.update_status.assert_not_awaited()


def test_exit_strategy_engine_module_has_no_forbidden_imports():
    if not ENGINE_MODULE_PATH.exists():
        pytest.fail(
            "Expected exit strategy engine implementation file at "
            "src/agents/execution/exit_strategy_engine.py.",
            pytrace=False,
        )

    tree = ast.parse(ENGINE_MODULE_PATH.read_text())
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
