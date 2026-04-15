"""
tests/unit/test_wi28_net_pnl.py

RED-phase unit tests for WI-28 Net PnL & Fee Accounting.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError


CALCULATOR_MODULE_NAME = "src.agents.execution.pnl_calculator"
EXECUTION_SCHEMA_MODULE_NAME = "src.schemas.execution"
POSITION_SCHEMA_MODULE_NAME = "src.schemas.position"
RISK_SCHEMA_MODULE_NAME = "src.schemas.risk"
REPORTER_MODULE_NAME = "src.agents.execution.lifecycle_reporter"


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


def _make_position_record_payload(
    position_schema_module,
    execution_schema_module,
) -> dict[str, object]:
    now = datetime.now(timezone.utc)
    return {
        "id": "pos-wi28-unit-001",
        "condition_id": "condition-wi28-unit-001",
        "token_id": "token-wi28-unit-001",
        "status": position_schema_module.PositionStatus.CLOSED,
        "side": "BUY",
        "entry_price": Decimal("0.45"),
        "order_size_usdc": Decimal("25"),
        "kelly_fraction": Decimal("0.10"),
        "best_ask_at_entry": Decimal("0.46"),
        "bankroll_usdc_at_entry": Decimal("1000"),
        "execution_action": execution_schema_module.ExecutionAction.EXECUTED,
        "reason": "unit-test",
        "routed_at_utc": now,
        "recorded_at_utc": now,
        "realized_pnl": None,
        "exit_price": None,
        "closed_at_utc": None,
    }


def _make_position_record(
    position_schema_module,
    execution_schema_module,
    *,
    entry_price: Decimal = Decimal("0.45"),
    order_size_usdc: Decimal = Decimal("25"),
):
    payload = _make_position_record_payload(
        position_schema_module,
        execution_schema_module,
    )
    payload["entry_price"] = entry_price
    payload["order_size_usdc"] = order_size_usdc
    return position_schema_module.PositionRecord(**payload)


def _make_pnl_record_payload() -> dict[str, object]:
    return {
        "position_id": "pos-wi28-unit-001",
        "condition_id": "condition-wi28-unit-001",
        "entry_price": Decimal("0.45"),
        "exit_price": Decimal("0.65"),
        "order_size_usdc": Decimal("25"),
        "position_size_tokens": Decimal("55.555555555555555555"),
        "realized_pnl": Decimal("11.111111111111111111"),
        "closed_at_utc": datetime.now(timezone.utc),
    }


def _make_lifecycle_entry_payload() -> dict[str, object]:
    now = datetime.now(timezone.utc)
    return {
        "position_id": "pos-wi28-entry-001",
        "slug": "condition-wi28-entry-001",
        "entry_price": Decimal("0.45"),
        "exit_price": Decimal("0.60"),
        "size_tokens": Decimal("10"),
        "realized_pnl": Decimal("5.0"),
        "status": "CLOSED",
        "opened_at_utc": now,
        "settled_at_utc": now,
    }


def _make_lifecycle_report_payload() -> dict[str, object]:
    return {
        "report_at_utc": datetime.now(timezone.utc),
        "total_settled_count": 1,
        "winning_count": 1,
        "losing_count": 0,
        "breakeven_count": 0,
        "total_realized_pnl": Decimal("5.0"),
        "avg_hold_duration_hours": Decimal("2"),
        "best_pnl": Decimal("5.0"),
        "worst_pnl": Decimal("5.0"),
        "entries": [],
        "dry_run": True,
    }


def _build_calculator(
    calculator_module,
    *,
    dry_run: bool,
    db_session_factory=None,
):
    if db_session_factory is None:
        db_session_factory = MagicMock()
    return calculator_module.PnLCalculator(
        config=SimpleNamespace(dry_run=dry_run),
        db_session_factory=db_session_factory,
    )


def _make_reporter_position(
    *,
    position_id: str,
    status: str,
    entry_price: Decimal,
    order_size_usdc: Decimal,
    realized_pnl: Decimal | None,
    gas_cost_usdc: Decimal | None,
    fees_usdc: Decimal | None,
    routed_at_utc: datetime,
    closed_at_utc: datetime | None = None,
    exit_price: Decimal | None = None,
):
    return SimpleNamespace(
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
        gas_cost_usdc=gas_cost_usdc,
        fees_usdc=fees_usdc,
    )


async def _generate_report(
    reporter_module,
    *,
    dry_run: bool = True,
    all_positions: list[object] | None = None,
):
    config = SimpleNamespace(dry_run=dry_run)
    session = MagicMock()
    db_session_factory = MagicMock(return_value=_SessionCtx(session=session))
    repo = MagicMock()
    repo.get_all_positions = AsyncMock(return_value=all_positions or [])
    original_repo_cls = reporter_module.PositionRepository
    reporter_module.PositionRepository = MagicMock(return_value=repo)
    try:
        reporter = reporter_module.PositionLifecycleReporter(
            config=config,
            db_session_factory=db_session_factory,
        )
        return await reporter.generate_report()
    finally:
        reporter_module.PositionRepository = original_repo_cls


@pytest.mark.parametrize("field_name", ["gas_cost_usdc", "fees_usdc"])
def test_position_record_rejects_float_fee_fields(field_name):
    position_schema_module = _load_module(POSITION_SCHEMA_MODULE_NAME)
    execution_schema_module = _load_module(EXECUTION_SCHEMA_MODULE_NAME)
    payload = _make_position_record_payload(
        position_schema_module,
        execution_schema_module,
    )
    payload[field_name] = 1.5 if field_name == "gas_cost_usdc" else 0.25

    with pytest.raises(ValidationError):
        position_schema_module.PositionRecord(**payload)


def test_position_record_accepts_decimal_and_none_fee_fields():
    position_schema_module = _load_module(POSITION_SCHEMA_MODULE_NAME)
    execution_schema_module = _load_module(EXECUTION_SCHEMA_MODULE_NAME)

    decimal_payload = _make_position_record_payload(
        position_schema_module,
        execution_schema_module,
    )
    decimal_payload["gas_cost_usdc"] = Decimal("1.5")
    decimal_payload["fees_usdc"] = Decimal("0.25")
    decimal_record = position_schema_module.PositionRecord(**decimal_payload)

    none_payload = _make_position_record_payload(
        position_schema_module,
        execution_schema_module,
    )
    none_payload["gas_cost_usdc"] = None
    none_payload["fees_usdc"] = None
    none_record = position_schema_module.PositionRecord(**none_payload)

    assert decimal_record.gas_cost_usdc == Decimal("1.5")
    assert decimal_record.fees_usdc == Decimal("0.25")
    assert none_record.gas_cost_usdc is None
    assert none_record.fees_usdc is None


@pytest.mark.parametrize("field_name", ["gas_cost_usdc", "fees_usdc"])
def test_pnl_record_rejects_float_fee_fields(field_name):
    execution_schema_module = _load_module(EXECUTION_SCHEMA_MODULE_NAME)
    payload = _make_pnl_record_payload()
    payload[field_name] = 1.0 if field_name == "gas_cost_usdc" else 0.5
    payload["net_realized_pnl"] = Decimal("9.611111111111111111")

    with pytest.raises(ValidationError):
        execution_schema_module.PnLRecord(**payload)


def test_pnl_record_accepts_decimal_fee_fields_and_net_realized_pnl():
    execution_schema_module = _load_module(EXECUTION_SCHEMA_MODULE_NAME)
    payload = _make_pnl_record_payload()
    payload["gas_cost_usdc"] = Decimal("1.0")
    payload["fees_usdc"] = Decimal("0.5")
    payload["net_realized_pnl"] = Decimal("3.5")

    record = execution_schema_module.PnLRecord(**payload)

    assert record.gas_cost_usdc == Decimal("1.0")
    assert record.fees_usdc == Decimal("0.5")
    assert record.net_realized_pnl == Decimal("3.5")


@pytest.mark.parametrize("field_name", ["gas_cost_usdc", "fees_usdc"])
def test_position_lifecycle_entry_rejects_float_fee_fields(field_name):
    risk_schema_module = _load_module(RISK_SCHEMA_MODULE_NAME)
    payload = _make_lifecycle_entry_payload()
    payload[field_name] = 1.0 if field_name == "gas_cost_usdc" else 0.5

    with pytest.raises(ValidationError):
        risk_schema_module.PositionLifecycleEntry(**payload)


@pytest.mark.parametrize(
    "field_name,float_value",
    [
        ("total_gas_cost_usdc", 1.0),
        ("total_fees_usdc", 0.5),
        ("total_net_realized_pnl", 3.0),
    ],
)
def test_lifecycle_report_rejects_float_fee_aggregate_fields(field_name, float_value):
    risk_schema_module = _load_module(RISK_SCHEMA_MODULE_NAME)
    payload = _make_lifecycle_report_payload()
    payload["total_gas_cost_usdc"] = Decimal("0")
    payload["total_fees_usdc"] = Decimal("0")
    payload["total_net_realized_pnl"] = Decimal("5.0")
    payload[field_name] = float_value

    with pytest.raises(ValidationError):
        risk_schema_module.LifecycleReport(**payload)


@pytest.mark.asyncio
async def test_settle_with_explicit_gas_and_fees_computes_net_realized_pnl():
    calculator_module = _load_module(CALCULATOR_MODULE_NAME)
    position_schema_module = _load_module(POSITION_SCHEMA_MODULE_NAME)
    execution_schema_module = _load_module(EXECUTION_SCHEMA_MODULE_NAME)
    position = _make_position_record(position_schema_module, execution_schema_module)
    calculator = _build_calculator(calculator_module, dry_run=True)

    record = await calculator.settle(
        position=position,
        exit_price=Decimal("0.70"),
        gas_cost_usdc=Decimal("0.50"),
        fees_usdc=Decimal("0.25"),
    )

    expected_gross = (Decimal("0.70") - Decimal("0.45")) * (
        Decimal("25") / Decimal("0.45")
    )
    assert record.realized_pnl == expected_gross
    assert record.gas_cost_usdc == Decimal("0.50")
    assert record.fees_usdc == Decimal("0.25")
    assert record.net_realized_pnl == expected_gross - Decimal("0.50") - Decimal("0.25")


@pytest.mark.asyncio
async def test_settle_defaults_missing_gas_and_fees_to_zero_for_legacy_compatibility():
    calculator_module = _load_module(CALCULATOR_MODULE_NAME)
    position_schema_module = _load_module(POSITION_SCHEMA_MODULE_NAME)
    execution_schema_module = _load_module(EXECUTION_SCHEMA_MODULE_NAME)
    position = _make_position_record(position_schema_module, execution_schema_module)
    calculator = _build_calculator(calculator_module, dry_run=True)

    record = await calculator.settle(position=position, exit_price=Decimal("0.70"))

    assert record.gas_cost_usdc == Decimal("0")
    assert record.fees_usdc == Decimal("0")
    assert record.net_realized_pnl == record.realized_pnl


@pytest.mark.asyncio
async def test_settle_with_zero_entry_price_produces_negative_net_cost_only_result():
    calculator_module = _load_module(CALCULATOR_MODULE_NAME)
    position_schema_module = _load_module(POSITION_SCHEMA_MODULE_NAME)
    execution_schema_module = _load_module(EXECUTION_SCHEMA_MODULE_NAME)
    position = _make_position_record(
        position_schema_module,
        execution_schema_module,
        entry_price=Decimal("0"),
    )
    calculator = _build_calculator(calculator_module, dry_run=True)

    record = await calculator.settle(
        position=position,
        exit_price=Decimal("0.70"),
        gas_cost_usdc=Decimal("1.0"),
        fees_usdc=Decimal("0.5"),
    )

    assert record.position_size_tokens == Decimal("0")
    assert record.realized_pnl == Decimal("0")
    assert record.net_realized_pnl == Decimal("-1.5")


@pytest.mark.asyncio
async def test_settle_with_only_gas_defaults_fees_to_zero():
    calculator_module = _load_module(CALCULATOR_MODULE_NAME)
    position_schema_module = _load_module(POSITION_SCHEMA_MODULE_NAME)
    execution_schema_module = _load_module(EXECUTION_SCHEMA_MODULE_NAME)
    position = _make_position_record(position_schema_module, execution_schema_module)
    calculator = _build_calculator(calculator_module, dry_run=True)

    record = await calculator.settle(
        position=position,
        exit_price=Decimal("0.70"),
        gas_cost_usdc=Decimal("2.0"),
        fees_usdc=None,
    )

    expected_gross = (Decimal("0.70") - Decimal("0.45")) * (
        Decimal("25") / Decimal("0.45")
    )
    assert record.gas_cost_usdc == Decimal("2.0")
    assert record.fees_usdc == Decimal("0")
    assert record.net_realized_pnl == expected_gross - Decimal("2.0")


@pytest.mark.asyncio
async def test_settle_with_only_fees_defaults_gas_to_zero():
    calculator_module = _load_module(CALCULATOR_MODULE_NAME)
    position_schema_module = _load_module(POSITION_SCHEMA_MODULE_NAME)
    execution_schema_module = _load_module(EXECUTION_SCHEMA_MODULE_NAME)
    position = _make_position_record(position_schema_module, execution_schema_module)
    calculator = _build_calculator(calculator_module, dry_run=True)

    record = await calculator.settle(
        position=position,
        exit_price=Decimal("0.70"),
        gas_cost_usdc=None,
        fees_usdc=Decimal("1.0"),
    )

    expected_gross = (Decimal("0.70") - Decimal("0.45")) * (
        Decimal("25") / Decimal("0.45")
    )
    assert record.gas_cost_usdc == Decimal("0")
    assert record.fees_usdc == Decimal("1.0")
    assert record.net_realized_pnl == expected_gross - Decimal("1.0")


@pytest.mark.asyncio
async def test_dry_run_settle_returns_fee_aware_record_without_persisting():
    calculator_module = _load_module(CALCULATOR_MODULE_NAME)
    position_schema_module = _load_module(POSITION_SCHEMA_MODULE_NAME)
    execution_schema_module = _load_module(EXECUTION_SCHEMA_MODULE_NAME)
    db_session_factory = MagicMock()
    calculator = _build_calculator(
        calculator_module,
        dry_run=True,
        db_session_factory=db_session_factory,
    )
    position = _make_position_record(position_schema_module, execution_schema_module)

    record = await calculator.settle(
        position=position,
        exit_price=Decimal("0.70"),
        gas_cost_usdc=Decimal("0.50"),
        fees_usdc=Decimal("0.25"),
    )

    assert record.gas_cost_usdc == Decimal("0.50")
    assert record.fees_usdc == Decimal("0.25")
    assert record.net_realized_pnl == record.realized_pnl - Decimal("0.50") - Decimal(
        "0.25"
    )
    db_session_factory.assert_not_called()


def test_position_lifecycle_entry_derives_net_realized_pnl_from_cost_fields():
    risk_schema_module = _load_module(RISK_SCHEMA_MODULE_NAME)
    payload = _make_lifecycle_entry_payload()
    payload["gas_cost_usdc"] = Decimal("0.50")
    payload["fees_usdc"] = Decimal("0.25")

    entry = risk_schema_module.PositionLifecycleEntry(**payload)

    assert entry.net_realized_pnl == Decimal("4.25")


def test_position_lifecycle_entry_keeps_net_realized_pnl_none_for_open_positions():
    risk_schema_module = _load_module(RISK_SCHEMA_MODULE_NAME)
    payload = _make_lifecycle_entry_payload()
    payload["exit_price"] = None
    payload["realized_pnl"] = None
    payload["status"] = "OPEN"
    payload["settled_at_utc"] = None
    payload["gas_cost_usdc"] = Decimal("0")
    payload["fees_usdc"] = Decimal("0")

    entry = risk_schema_module.PositionLifecycleEntry(**payload)

    assert entry.net_realized_pnl is None


def test_position_lifecycle_entry_preserves_legacy_identity_when_costs_are_zero():
    risk_schema_module = _load_module(RISK_SCHEMA_MODULE_NAME)
    payload = _make_lifecycle_entry_payload()
    payload["realized_pnl"] = Decimal("3.0")
    payload["gas_cost_usdc"] = Decimal("0")
    payload["fees_usdc"] = Decimal("0")

    entry = risk_schema_module.PositionLifecycleEntry(**payload)

    assert entry.net_realized_pnl == Decimal("3.0")


@pytest.mark.asyncio
async def test_lifecycle_report_aggregates_gas_fees_and_net_realized_pnl():
    reporter_module = _load_module(REPORTER_MODULE_NAME)
    now = datetime.now(timezone.utc)
    positions = [
        _make_reporter_position(
            position_id="pos-wi28-report-001",
            status="CLOSED",
            entry_price=Decimal("0.50"),
            order_size_usdc=Decimal("10"),
            realized_pnl=Decimal("5.0"),
            gas_cost_usdc=Decimal("0.50"),
            fees_usdc=Decimal("0.25"),
            routed_at_utc=now,
            closed_at_utc=now,
            exit_price=Decimal("0.75"),
        ),
        _make_reporter_position(
            position_id="pos-wi28-report-002",
            status="CLOSED",
            entry_price=Decimal("0.40"),
            order_size_usdc=Decimal("8"),
            realized_pnl=Decimal("-1.0"),
            gas_cost_usdc=Decimal("0.10"),
            fees_usdc=Decimal("0.05"),
            routed_at_utc=now,
            closed_at_utc=now,
            exit_price=Decimal("0.35"),
        ),
    ]
    report = await _generate_report(reporter_module, all_positions=positions)

    assert report.total_gas_cost_usdc == Decimal("0.60")
    assert report.total_fees_usdc == Decimal("0.30")
    assert report.total_net_realized_pnl == Decimal("3.10")


@pytest.mark.asyncio
async def test_empty_lifecycle_report_initializes_fee_aggregates_to_zero():
    reporter_module = _load_module(REPORTER_MODULE_NAME)
    report = await _generate_report(reporter_module, all_positions=[])

    assert report.total_gas_cost_usdc == Decimal("0")
    assert report.total_fees_usdc == Decimal("0")
    assert report.total_net_realized_pnl == Decimal("0")
