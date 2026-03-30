"""
tests/unit/test_exit_scan_loop.py

RED-phase unit tests for WI-22 periodic exit scan loop wiring.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.config import AppConfig
from src.core.exceptions import ExitEvaluationError
from src.orchestrator import Orchestrator
from src.schemas.execution import ExitReason, ExitResult


ORCHESTRATOR_MODULE_PATH = Path("src/orchestrator.py")


def _patch_heavy_deps():
    """Neutralize network-bound orchestrator constructor deps."""
    mock_w3 = MagicMock()
    mock_w3.eth = MagicMock()
    mock_w3.eth.get_transaction_count = AsyncMock(return_value=0)
    return {
        "AsyncWeb3": MagicMock(return_value=mock_w3),
        "AsyncHTTPProvider": MagicMock(),
        "AsyncSessionLocal": MagicMock(),
        "engine": MagicMock(dispose=AsyncMock()),
    }


def _build_orchestrator(test_config) -> Orchestrator:
    with patch.multiple("src.orchestrator", **_patch_heavy_deps()):
        return Orchestrator(test_config)


def _make_exit_result(*, position_id: str, should_exit: bool) -> ExitResult:
    return ExitResult(
        position_id=position_id,
        condition_id=f"condition-{position_id}",
        should_exit=should_exit,
        exit_reason=ExitReason.STOP_LOSS if should_exit else ExitReason.NO_EDGE,
        entry_price=Decimal("0.65"),
        current_midpoint=Decimal("0.50"),
        current_best_bid=Decimal("0.49"),
        position_age_hours=Decimal("8"),
        unrealized_edge=Decimal("-0.15") if should_exit else Decimal("0.02"),
        evaluated_at_utc=datetime.now(timezone.utc),
    )


def test_app_config_includes_exit_scan_interval_seconds_decimal_default():
    fields = AppConfig.model_fields

    assert "exit_scan_interval_seconds" in fields, (
        "Expected AppConfig.exit_scan_interval_seconds field."
    )
    field = fields["exit_scan_interval_seconds"]
    assert field.annotation is Decimal
    assert field.default == Decimal("60")


def test_app_config_accepts_exit_scan_interval_seconds_env_override(monkeypatch):
    monkeypatch.setenv("EXIT_SCAN_INTERVAL_SECONDS", "120")
    cfg = AppConfig()

    assert isinstance(cfg.exit_scan_interval_seconds, Decimal)
    assert cfg.exit_scan_interval_seconds == Decimal("120")


def test_orchestrator_exposes_async_exit_scan_loop_method():
    assert hasattr(Orchestrator, "_exit_scan_loop")
    assert inspect.iscoroutinefunction(Orchestrator._exit_scan_loop)


@pytest.mark.asyncio
async def test_exit_scan_loop_sleeps_first_then_scans(monkeypatch, test_config):
    orchestrator = _build_orchestrator(test_config)
    object.__setattr__(orchestrator.config, "exit_scan_interval_seconds", Decimal("7"))

    call_order: list[tuple[str, float | None]] = []
    sleep_calls = 0

    async def _fake_sleep(seconds: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        call_order.append(("sleep", seconds))
        if sleep_calls >= 2:
            raise asyncio.CancelledError

    async def _fake_scan():
        call_order.append(("scan", None))
        return [_make_exit_result(position_id="pos-001", should_exit=False)]

    orchestrator.exit_strategy_engine.scan_open_positions = AsyncMock(
        side_effect=_fake_scan
    )
    mock_logger = MagicMock()
    monkeypatch.setattr("src.orchestrator.logger", mock_logger)
    monkeypatch.setattr("src.orchestrator.asyncio.sleep", _fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await orchestrator._exit_scan_loop()

    assert call_order[0] == ("sleep", float(Decimal("7")))
    assert call_order[1][0] == "scan"
    mock_logger.info.assert_any_call(
        "exit_scan_loop.completed",
        total=1,
        exits=0,
        holds=1,
        interval_seconds="7",
    )


@pytest.mark.asyncio
async def test_exit_scan_loop_catches_generic_exception_and_continues(
    monkeypatch, test_config
):
    orchestrator = _build_orchestrator(test_config)
    object.__setattr__(
        orchestrator.config, "exit_scan_interval_seconds", Decimal("0.5")
    )

    sleep_calls = 0

    async def _fake_sleep(seconds: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= 3:
            raise asyncio.CancelledError

    orchestrator.exit_strategy_engine.scan_open_positions = AsyncMock(
        side_effect=[
            Exception("scan exploded"),
            [_make_exit_result(position_id="pos-002", should_exit=False)],
        ]
    )
    mock_logger = MagicMock()
    monkeypatch.setattr("src.orchestrator.logger", mock_logger)
    monkeypatch.setattr("src.orchestrator.asyncio.sleep", _fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await orchestrator._exit_scan_loop()

    assert orchestrator.exit_strategy_engine.scan_open_positions.await_count == 2
    mock_logger.error.assert_any_call(
        "exit_scan_loop.error",
        error="scan exploded",
    )
    mock_logger.info.assert_any_call(
        "exit_scan_loop.completed",
        total=1,
        exits=0,
        holds=1,
        interval_seconds="0.5",
    )


@pytest.mark.asyncio
async def test_exit_scan_loop_catches_exit_evaluation_error_and_continues(
    monkeypatch, test_config
):
    orchestrator = _build_orchestrator(test_config)
    object.__setattr__(orchestrator.config, "exit_scan_interval_seconds", Decimal("3"))

    sleep_calls = 0

    async def _fake_sleep(seconds: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= 3:
            raise asyncio.CancelledError

    orchestrator.exit_strategy_engine.scan_open_positions = AsyncMock(
        side_effect=[
            ExitEvaluationError(reason="open_position_scan_failed"),
            [_make_exit_result(position_id="pos-003", should_exit=True)],
        ]
    )
    mock_logger = MagicMock()
    monkeypatch.setattr("src.orchestrator.logger", mock_logger)
    monkeypatch.setattr("src.orchestrator.asyncio.sleep", _fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await orchestrator._exit_scan_loop()

    assert orchestrator.exit_strategy_engine.scan_open_positions.await_count == 2
    mock_logger.error.assert_any_call(
        "exit_scan_loop.error",
        error="open_position_scan_failed",
    )
    mock_logger.info.assert_any_call(
        "exit_scan_loop.completed",
        total=1,
        exits=1,
        holds=0,
        interval_seconds="3",
    )


@pytest.mark.asyncio
async def test_exit_scan_loop_logs_completed_counts(monkeypatch, test_config):
    orchestrator = _build_orchestrator(test_config)
    object.__setattr__(
        orchestrator.config, "exit_scan_interval_seconds", Decimal("13")
    )

    sleep_calls = 0

    async def _fake_sleep(seconds: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= 2:
            raise asyncio.CancelledError

    orchestrator.exit_strategy_engine.scan_open_positions = AsyncMock(
        return_value=[
            _make_exit_result(position_id="pos-004", should_exit=True),
            _make_exit_result(position_id="pos-005", should_exit=True),
            _make_exit_result(position_id="pos-006", should_exit=False),
        ]
    )
    mock_logger = MagicMock()
    monkeypatch.setattr("src.orchestrator.logger", mock_logger)
    monkeypatch.setattr("src.orchestrator.asyncio.sleep", _fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await orchestrator._exit_scan_loop()

    mock_logger.info.assert_any_call(
        "exit_scan_loop.completed",
        total=3,
        exits=2,
        holds=1,
        interval_seconds="13",
    )


def test_execution_consumer_loop_has_no_inline_exit_scan_references():
    assert ORCHESTRATOR_MODULE_PATH.exists()

    source = ORCHESTRATOR_MODULE_PATH.read_text()
    tree = ast.parse(source)
    consumer_node = None

    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "Orchestrator":
            for child in node.body:
                if (
                    isinstance(child, ast.AsyncFunctionDef)
                    and child.name == "_execution_consumer_loop"
                ):
                    consumer_node = child
                    break

    assert consumer_node is not None
    consumer_source = ast.get_source_segment(source, consumer_node) or ""
    assert "scan_open_positions" not in consumer_source
    assert "exit_strategy_engine" not in consumer_source
