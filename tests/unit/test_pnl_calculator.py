"""
tests/unit/test_pnl_calculator.py

RED-phase unit tests for WI-21 Realized PnL & Settlement.
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


CALCULATOR_MODULE_NAME = "src.agents.execution.pnl_calculator"
CALCULATOR_MODULE_PATH = Path("src/agents/execution/pnl_calculator.py")
SCHEMA_MODULE_NAME = "src.schemas.execution"
POSITION_SCHEMA_MODULE_NAME = "src.schemas.position"
FORBIDDEN_IMPORT_PREFIXES = (
    "src.agents.context",
    "src.agents.evaluation",
    "src.agents.ingestion",
)
FORBIDDEN_IMPORTS = {
    "src.agents.execution.polymarket_client",
    "src.agents.execution.bankroll_sync",
    "src.agents.execution.signer",
    "src.agents.execution.execution_router",
    "src.agents.execution.exit_order_router",
}


def _load_module(name: str):
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError:
        pytest.fail(f"Expected module {name} to exist.", pytrace=False)
    except Exception as exc:
        pytest.fail(f"Module {name} import failed unexpectedly: {exc!r}", pytrace=False)


def _make_position_record(
    position_schema_module,
    execution_schema_module,
    *,
    position_id: str = "pos-unit-001",
    condition_id: str = "condition-unit-001",
    entry_price: Decimal = Decimal("0.45"),
    order_size_usdc: Decimal = Decimal("25"),
):
    now = datetime.now(timezone.utc)
    return position_schema_module.PositionRecord(
        id=position_id,
        condition_id=condition_id,
        token_id="token-yes-001",
        status=position_schema_module.PositionStatus.CLOSED,
        side="BUY",
        entry_price=entry_price,
        order_size_usdc=order_size_usdc,
        kelly_fraction=Decimal("0.10"),
        best_ask_at_entry=entry_price + Decimal("0.01"),
        bankroll_usdc_at_entry=Decimal("1000"),
        execution_action=execution_schema_module.ExecutionAction.EXECUTED,
        reason="unit-test",
        routed_at_utc=now,
        recorded_at_utc=now,
        realized_pnl=None,
        exit_price=None,
        closed_at_utc=None,
    )


def _build_calculator(
    calculator_module,
    *,
    dry_run: bool,
    db_session_factory=None,
):
    if db_session_factory is None:
        db_session_factory = MagicMock()
    config = SimpleNamespace(dry_run=dry_run)
    return calculator_module.PnLCalculator(
        config=config,
        db_session_factory=db_session_factory,
    )


def test_pnl_calculator_contract_exists_and_has_one_public_async_method():
    calculator_module = _load_module(CALCULATOR_MODULE_NAME)
    calculator_cls = getattr(calculator_module, "PnLCalculator", None)

    assert calculator_cls is not None, "Expected PnLCalculator class."
    assert inspect.isclass(calculator_cls)
    assert inspect.iscoroutinefunction(calculator_cls.settle)

    init_params = list(inspect.signature(calculator_cls.__init__).parameters.keys())
    assert init_params == ["self", "config", "db_session_factory"]

    public_methods = [
        name
        for name, member in inspect.getmembers(calculator_cls, predicate=inspect.isfunction)
        if not name.startswith("_")
    ]
    assert public_methods == ["settle"]

    settle_sig = inspect.signature(calculator_cls.settle)
    assert list(settle_sig.parameters.keys()) == ["self", "position", "exit_price"]


def test_pnl_record_schema_exists_with_expected_fields_and_is_frozen():
    schema_module = _load_module(SCHEMA_MODULE_NAME)
    pnl_cls = getattr(schema_module, "PnLRecord", None)
    assert pnl_cls is not None, "Expected PnLRecord model in src.schemas.execution."

    assert {
        "position_id",
        "condition_id",
        "entry_price",
        "exit_price",
        "order_size_usdc",
        "position_size_tokens",
        "realized_pnl",
        "closed_at_utc",
    }.issubset(pnl_cls.model_fields.keys())

    record = pnl_cls(
        position_id="pos-unit-001",
        condition_id="condition-unit-001",
        entry_price=Decimal("0.45"),
        exit_price=Decimal("0.65"),
        order_size_usdc=Decimal("25"),
        position_size_tokens=Decimal("55.555555555555555555"),
        realized_pnl=Decimal("11.111111111111111111"),
        closed_at_utc=datetime.now(timezone.utc),
    )
    with pytest.raises(Exception):
        record.realized_pnl = Decimal("0")


@pytest.mark.parametrize(
    "field_name",
    [
        "entry_price",
        "exit_price",
        "order_size_usdc",
        "position_size_tokens",
        "realized_pnl",
    ],
)
def test_pnl_record_rejects_float_financial_fields(field_name):
    schema_module = _load_module(SCHEMA_MODULE_NAME)
    pnl_cls = getattr(schema_module, "PnLRecord", None)
    assert pnl_cls is not None, "Expected PnLRecord model in src.schemas.execution."

    payload = {
        "position_id": "pos-unit-001",
        "condition_id": "condition-unit-001",
        "entry_price": Decimal("0.45"),
        "exit_price": Decimal("0.65"),
        "order_size_usdc": Decimal("25"),
        "position_size_tokens": Decimal("55.555555555555555555"),
        "realized_pnl": Decimal("11.111111111111111111"),
        "closed_at_utc": datetime.now(timezone.utc),
    }
    payload[field_name] = 0.1

    with pytest.raises(Exception):
        pnl_cls(**payload)


def test_position_record_accepts_none_for_wi21_optional_settlement_fields():
    position_schema_module = _load_module(POSITION_SCHEMA_MODULE_NAME)
    execution_schema_module = _load_module(SCHEMA_MODULE_NAME)
    record = _make_position_record(position_schema_module, execution_schema_module)

    assert record.realized_pnl is None
    assert record.exit_price is None
    assert record.closed_at_utc is None


@pytest.mark.parametrize("field_name", ["realized_pnl", "exit_price"])
def test_position_record_rejects_float_for_wi21_optional_settlement_fields(field_name):
    schema_module = _load_module(POSITION_SCHEMA_MODULE_NAME)
    now = datetime.now(timezone.utc)
    payload = {
        "id": "pos-unit-001",
        "condition_id": "condition-unit-001",
        "token_id": "token-yes-001",
        "status": schema_module.PositionStatus.CLOSED,
        "side": "BUY",
        "entry_price": Decimal("0.45"),
        "order_size_usdc": Decimal("25"),
        "kelly_fraction": Decimal("0.10"),
        "best_ask_at_entry": Decimal("0.46"),
        "bankroll_usdc_at_entry": Decimal("1000"),
        "execution_action": _load_module(SCHEMA_MODULE_NAME).ExecutionAction.EXECUTED,
        "reason": "unit-test",
        "routed_at_utc": now,
        "recorded_at_utc": now,
        "realized_pnl": None,
        "exit_price": None,
        "closed_at_utc": None,
    }
    payload[field_name] = 0.1

    with pytest.raises(Exception):
        schema_module.PositionRecord(**payload)


@pytest.mark.asyncio
async def test_pnl_formula_profit_path_and_token_quantity_derivation():
    calculator_module = _load_module(CALCULATOR_MODULE_NAME)
    schema_module = _load_module(POSITION_SCHEMA_MODULE_NAME)
    execution_module = _load_module(SCHEMA_MODULE_NAME)
    position = _make_position_record(
        schema_module,
        execution_module,
        entry_price=Decimal("0.45"),
        order_size_usdc=Decimal("25"),
    )
    calculator = _build_calculator(calculator_module, dry_run=True)

    record = await calculator.settle(position=position, exit_price=Decimal("0.65"))

    expected_size_tokens = Decimal("25") / Decimal("0.45")
    expected_pnl = (Decimal("0.65") - Decimal("0.45")) * expected_size_tokens
    assert record.position_size_tokens == expected_size_tokens
    assert record.realized_pnl == expected_pnl


@pytest.mark.asyncio
async def test_pnl_formula_loss_path():
    calculator_module = _load_module(CALCULATOR_MODULE_NAME)
    schema_module = _load_module(POSITION_SCHEMA_MODULE_NAME)
    execution_module = _load_module(SCHEMA_MODULE_NAME)
    position = _make_position_record(
        schema_module,
        execution_module,
        entry_price=Decimal("0.65"),
        order_size_usdc=Decimal("25"),
    )
    calculator = _build_calculator(calculator_module, dry_run=True)

    record = await calculator.settle(position=position, exit_price=Decimal("0.45"))

    assert record.realized_pnl < Decimal("0")


@pytest.mark.asyncio
async def test_pnl_formula_breakeven_path():
    calculator_module = _load_module(CALCULATOR_MODULE_NAME)
    schema_module = _load_module(POSITION_SCHEMA_MODULE_NAME)
    execution_module = _load_module(SCHEMA_MODULE_NAME)
    position = _make_position_record(
        schema_module,
        execution_module,
        entry_price=Decimal("0.45"),
        order_size_usdc=Decimal("25"),
    )
    calculator = _build_calculator(calculator_module, dry_run=True)

    record = await calculator.settle(position=position, exit_price=Decimal("0.45"))

    assert record.realized_pnl == Decimal("0")


@pytest.mark.asyncio
async def test_degenerate_entry_price_logs_warning_and_returns_zero_realized_pnl(
    monkeypatch,
):
    calculator_module = _load_module(CALCULATOR_MODULE_NAME)
    schema_module = _load_module(POSITION_SCHEMA_MODULE_NAME)
    execution_module = _load_module(SCHEMA_MODULE_NAME)
    position = _make_position_record(
        schema_module,
        execution_module,
        entry_price=Decimal("0"),
        order_size_usdc=Decimal("25"),
    )
    calculator = _build_calculator(calculator_module, dry_run=True)

    mock_logger = MagicMock()
    monkeypatch.setattr(calculator_module, "logger", mock_logger)

    record = await calculator.settle(position=position, exit_price=Decimal("0.65"))

    assert record.position_size_tokens == Decimal("0")
    assert record.realized_pnl == Decimal("0")
    mock_logger.warning.assert_any_call(
        "pnl.degenerate_entry_price",
        position_id=position.id,
        condition_id=position.condition_id,
        entry_price="0",
    )


@pytest.mark.asyncio
async def test_dry_run_computes_record_logs_and_creates_zero_sessions(monkeypatch):
    calculator_module = _load_module(CALCULATOR_MODULE_NAME)
    schema_module = _load_module(POSITION_SCHEMA_MODULE_NAME)
    execution_module = _load_module(SCHEMA_MODULE_NAME)

    mock_logger = MagicMock()
    monkeypatch.setattr(calculator_module, "logger", mock_logger)
    db_session_factory = MagicMock()
    calculator = _build_calculator(
        calculator_module,
        dry_run=True,
        db_session_factory=db_session_factory,
    )
    position = _make_position_record(schema_module, execution_module)

    record = await calculator.settle(position=position, exit_price=Decimal("0.65"))

    assert record.position_id == position.id
    db_session_factory.assert_not_called()
    mock_logger.info.assert_any_call(
        "pnl.dry_run_settlement",
        position_id=position.id,
        condition_id=position.condition_id,
        realized_pnl=str(record.realized_pnl),
        exit_price="0.65",
    )


@pytest.mark.asyncio
async def test_live_settle_calls_repository_record_settlement_and_commit(monkeypatch):
    calculator_module = _load_module(CALCULATOR_MODULE_NAME)
    schema_module = _load_module(POSITION_SCHEMA_MODULE_NAME)
    execution_module = _load_module(SCHEMA_MODULE_NAME)
    position = _make_position_record(schema_module, execution_module)

    class _SessionCtx:
        def __init__(self):
            self.session = MagicMock()
            self.session.commit = AsyncMock()

        async def __aenter__(self):
            return self.session

        async def __aexit__(self, exc_type, exc, tb):
            return False

    session_ctx = _SessionCtx()
    db_session_factory = MagicMock(return_value=session_ctx)
    repo = MagicMock()
    repo.record_settlement = AsyncMock(return_value=MagicMock())
    repo_cls = MagicMock(return_value=repo)
    monkeypatch.setattr(calculator_module, "PositionRepository", repo_cls)

    calculator = _build_calculator(
        calculator_module,
        dry_run=False,
        db_session_factory=db_session_factory,
    )
    record = await calculator.settle(position=position, exit_price=Decimal("0.65"))

    assert record.position_id == position.id
    db_session_factory.assert_called_once()
    repo.record_settlement.assert_awaited_once()
    session_ctx.session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_live_settle_raises_pnl_calculation_error_when_position_not_found(
    monkeypatch,
):
    calculator_module = _load_module(CALCULATOR_MODULE_NAME)
    schema_module = _load_module(POSITION_SCHEMA_MODULE_NAME)
    execution_module = _load_module(SCHEMA_MODULE_NAME)
    exceptions_module = _load_module("src.core.exceptions")
    position = _make_position_record(schema_module, execution_module)

    class _SessionCtx:
        async def __aenter__(self):
            return MagicMock(commit=AsyncMock())

        async def __aexit__(self, exc_type, exc, tb):
            return False

    db_session_factory = MagicMock(return_value=_SessionCtx())
    repo = MagicMock()
    repo.record_settlement = AsyncMock(return_value=None)
    monkeypatch.setattr(calculator_module, "PositionRepository", MagicMock(return_value=repo))

    calculator = _build_calculator(
        calculator_module,
        dry_run=False,
        db_session_factory=db_session_factory,
    )

    with pytest.raises(exceptions_module.PnLCalculationError):
        await calculator.settle(position=position, exit_price=Decimal("0.65"))


@pytest.mark.asyncio
async def test_live_settle_wraps_db_failures_as_pnl_calculation_error(monkeypatch):
    calculator_module = _load_module(CALCULATOR_MODULE_NAME)
    schema_module = _load_module(POSITION_SCHEMA_MODULE_NAME)
    execution_module = _load_module(SCHEMA_MODULE_NAME)
    exceptions_module = _load_module("src.core.exceptions")
    position = _make_position_record(schema_module, execution_module)

    class _SessionCtx:
        async def __aenter__(self):
            return MagicMock(commit=AsyncMock(side_effect=RuntimeError("boom")))

        async def __aexit__(self, exc_type, exc, tb):
            return False

    db_session_factory = MagicMock(return_value=_SessionCtx())
    repo = MagicMock()
    repo.record_settlement = AsyncMock(return_value=MagicMock())
    monkeypatch.setattr(calculator_module, "PositionRepository", MagicMock(return_value=repo))

    calculator = _build_calculator(
        calculator_module,
        dry_run=False,
        db_session_factory=db_session_factory,
    )

    with pytest.raises(exceptions_module.PnLCalculationError):
        await calculator.settle(position=position, exit_price=Decimal("0.65"))


def test_pnl_calculator_module_import_boundary():
    if not CALCULATOR_MODULE_PATH.exists():
        pytest.fail(
            "Expected implementation file at src/agents/execution/pnl_calculator.py.",
            pytrace=False,
        )

    tree = ast.parse(CALCULATOR_MODULE_PATH.read_text())
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
