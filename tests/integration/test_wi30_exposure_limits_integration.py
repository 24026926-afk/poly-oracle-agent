"""
tests/integration/test_wi30_exposure_limits_integration.py

RED-phase integration tests for WI-30 Global Portfolio Exposure Limits.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime, timezone
from decimal import Decimal
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.db.models import Position
from src.db.repositories.position_repository import PositionRepository
from src.orchestrator import Orchestrator
from src.schemas.execution import ExecutionAction, ExecutionResult
from src.schemas.llm import MarketCategory


def _load_module(module_name: str):
    try:
        return importlib.import_module(module_name)
    except Exception as exc:  # pragma: no cover - RED diagnostics
        pytest.fail(f"Failed to import {module_name}: {exc!r}", pytrace=False)


def _patch_heavy_deps(*, db_session_factory=None):
    mock_w3 = MagicMock()
    mock_w3.eth = MagicMock()
    mock_w3.eth.get_transaction_count = AsyncMock(return_value=0)
    return {
        "AsyncWeb3": MagicMock(return_value=mock_w3),
        "AsyncHTTPProvider": MagicMock(),
        "AsyncSessionLocal": db_session_factory or MagicMock(),
        "engine": MagicMock(dispose=AsyncMock()),
    }


def _make_open_position(*, position_id: str, order_size_usdc: Decimal) -> Position:
    now = datetime.now(timezone.utc)
    return Position(
        id=position_id,
        condition_id=f"cond-{position_id}",
        token_id=f"token-{position_id}",
        status="OPEN",
        side="BUY",
        entry_price=Decimal("0.55"),
        order_size_usdc=order_size_usdc,
        kelly_fraction=Decimal("0.10"),
        best_ask_at_entry=Decimal("0.56"),
        bankroll_usdc_at_entry=Decimal("1000"),
        execution_action="DRY_RUN",
        reason="wi30-red",
        routed_at_utc=now,
        recorded_at_utc=now,
    )


def _make_closed_position(*, position_id: str, order_size_usdc: Decimal) -> Position:
    now = datetime.now(timezone.utc)
    return Position(
        id=position_id,
        condition_id=f"cond-{position_id}",
        token_id=f"token-{position_id}",
        status="CLOSED",
        side="BUY",
        entry_price=Decimal("0.55"),
        order_size_usdc=order_size_usdc,
        kelly_fraction=Decimal("0.10"),
        best_ask_at_entry=Decimal("0.56"),
        bankroll_usdc_at_entry=Decimal("1000"),
        execution_action="EXECUTED",
        reason="wi30-red",
        routed_at_utc=now,
        recorded_at_utc=now,
    )


def _make_eval_response(condition_id: str = "cond-wi30-int-001") -> MagicMock:
    eval_resp = MagicMock()
    eval_resp.market_context.condition_id = condition_id
    eval_resp.market_context.yes_token_id = "yes-token-wi30-int"
    eval_resp.recommended_action.value = "BUY"
    eval_resp.position_size_pct = Decimal("0.02")
    return eval_resp


async def _run_execution_consumer_once(orch: Orchestrator, item: dict) -> None:
    await orch.execution_queue.put(item)
    task = asyncio.create_task(orch._execution_consumer_loop())
    await asyncio.sleep(0.05)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_exposure_validator_uses_sqlite_open_positions_sum_for_limit_check(
    db_session_factory,
):
    module = _load_module("src.agents.execution.exposure_validator")

    async with db_session_factory() as session:
        repo = PositionRepository(session)
        await repo.insert_position(
            _make_open_position(
                position_id="pos-open-001", order_size_usdc=Decimal("10")
            )
        )
        await repo.insert_position(
            _make_open_position(
                position_id="pos-open-002", order_size_usdc=Decimal("18")
            )
        )
        await repo.insert_position(
            _make_closed_position(
                position_id="pos-closed-001", order_size_usdc=Decimal("100")
            )
        )
        await session.commit()

        open_positions = await repo.get_open_positions()
        current_exposure_usdc = sum(
            (Decimal(str(row.order_size_usdc)) for row in open_positions),
            Decimal("0"),
        )
        assert current_exposure_usdc == Decimal("28")

        validator = module.ExposureValidator(
            config=SimpleNamespace(
                max_exposure_pct=Decimal("0.03"),
                max_category_exposure_pct=Decimal("0.015"),
            ),
            position_repo=repo,
        )
        passed, summary = validator.validate_entry(
            bankroll_usdc=Decimal("1000"),
            proposed_size_usdc=Decimal("5"),
            category=MarketCategory.GENERAL,
            open_positions=open_positions,
        )

    assert passed is False
    assert summary.aggregate_exposure_usdc == Decimal("28")
    assert summary.validation_passed is False


@pytest.mark.asyncio
async def test_execution_consumer_rejects_trade_when_exposure_limit_breached(
    test_config,
    db_session_factory,
):
    cfg = test_config.model_copy(deep=True)
    object.__setattr__(cfg, "enable_exposure_validator", True)
    object.__setattr__(cfg, "gas_check_enabled", False)

    with patch.multiple(
        "src.orchestrator",
        **_patch_heavy_deps(db_session_factory=db_session_factory),
    ):
        orch = Orchestrator(cfg)

    orch.broadcaster = AsyncMock()
    orch.position_tracker.record_execution = AsyncMock(return_value=None)
    orch.execution_router.route = AsyncMock(
        return_value=ExecutionResult(action=ExecutionAction.DRY_RUN)
    )

    summary = MagicMock()
    summary.aggregate_exposure_usdc = Decimal("28")
    summary.available_headroom_usdc = Decimal("2")
    summary.model_dump.return_value = {}
    orch._position_repo = MagicMock()
    orch._position_repo.get_open_positions = AsyncMock(
        return_value=[SimpleNamespace(order_size_usdc=Decimal("28"))]
    )
    orch._exposure_validator = MagicMock()
    orch._exposure_validator.validate_entry.return_value = (False, summary)

    item = {
        "evaluation": _make_eval_response(),
        "snapshot_id": "snap-wi30-int-001",
        "yes_token_id": "token-wi30-int-001",
        "proposed_size_usdc": Decimal("5"),
        "category": MarketCategory.CRYPTO,
    }
    await _run_execution_consumer_once(orch, item)

    assert "execution_result" in item
    assert item["execution_result"].action == ExecutionAction.SKIP
    assert item["execution_result"].reason == "exposure_limit_exceeded"
    orch.execution_router.route.assert_not_awaited()
    orch._position_repo.get_open_positions.assert_awaited_once()
    orch._exposure_validator.validate_entry.assert_called_once()
