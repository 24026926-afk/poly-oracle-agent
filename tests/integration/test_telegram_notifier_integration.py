"""
tests/integration/test_telegram_notifier_integration.py

RED-phase integration tests for WI-26 Telegram notifier wiring.
"""

from __future__ import annotations

import ast
import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import importlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from pydantic import SecretStr

from src.orchestrator import Orchestrator
from src.schemas.execution import (
    ExecutionAction,
    ExecutionResult,
    ExitOrderAction,
    ExitOrderResult,
    ExitReason,
    ExitResult,
    PositionRecord,
    PositionStatus,
)
from src.schemas.risk import AlertEvent, AlertSeverity, LifecycleReport, PortfolioSnapshot
from src.schemas.web3 import OrderData, OrderSide, SIGNATURE_TYPE_EOA, SignedOrder


TELEGRAM_MODULE_NAME = "src.agents.execution.telegram_notifier"
TELEGRAM_MODULE_PATH = Path("src/agents/execution/telegram_notifier.py")
FORBIDDEN_IMPORT_PREFIXES = (
    "src.agents.context",
    "src.agents.evaluation",
    "src.agents.ingestion",
    "src.db",
    "sqlalchemy",
)
FORBIDDEN_IMPORTS = {
    "src.agents.execution.alert_engine",
    "src.agents.execution.portfolio_aggregator",
    "src.agents.execution.lifecycle_reporter",
    "src.agents.execution.exit_strategy_engine",
    "src.agents.execution.exit_order_router",
    "src.agents.execution.pnl_calculator",
    "src.agents.execution.execution_router",
    "src.agents.execution.broadcaster",
    "src.agents.execution.signer",
    "src.agents.execution.bankroll_sync",
    "src.agents.execution.polymarket_client",
}
WALLET_ADDRESS = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"


def _load_module(name: str):
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError:
        pytest.fail(f"Expected module {name} to exist.", pytrace=False)
    except Exception as exc:
        pytest.fail(f"Module {name} import failed unexpectedly: {exc!r}", pytrace=False)


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


def _build_orchestrator(
    test_config,
    *,
    db_session_factory=None,
    telegram_http_client=None,
):
    patches = _patch_heavy_deps(db_session_factory=db_session_factory)
    patchers = [patch.multiple("src.orchestrator", **patches)]

    if telegram_http_client is not None:
        patchers.append(
            patch(
                "src.orchestrator.httpx.AsyncClient",
                MagicMock(return_value=telegram_http_client),
            )
        )

    with patchers[0]:
        if len(patchers) == 1:
            return Orchestrator(test_config)
        with patchers[1]:
            return Orchestrator(test_config)


def _make_alert(
    *,
    severity: AlertSeverity = AlertSeverity.CRITICAL,
    rule_name: str = "drawdown",
    message: str = "Drawdown threshold exceeded",
    dry_run: bool = True,
) -> AlertEvent:
    return AlertEvent(
        alert_at_utc=datetime.now(timezone.utc),
        severity=severity,
        rule_name=rule_name,
        message=message,
        threshold_value=Decimal("100"),
        actual_value=Decimal("-140"),
        dry_run=dry_run,
    )


def _make_snapshot(*, dry_run: bool) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        snapshot_at_utc=datetime.now(timezone.utc),
        position_count=2,
        total_notional_usdc=Decimal("100"),
        total_unrealized_pnl=Decimal("-140"),
        total_locked_collateral_usdc=Decimal("80"),
        positions_with_stale_price=0,
        dry_run=dry_run,
    )


def _make_report(*, dry_run: bool) -> LifecycleReport:
    return LifecycleReport(
        report_at_utc=datetime.now(timezone.utc),
        total_settled_count=4,
        winning_count=1,
        losing_count=3,
        breakeven_count=0,
        total_realized_pnl=Decimal("-20"),
        avg_hold_duration_hours=Decimal("5"),
        best_pnl=Decimal("2"),
        worst_pnl=Decimal("-9"),
        entries=[],
        dry_run=dry_run,
    )


def _make_execution_result(*, action: ExecutionAction) -> ExecutionResult:
    signed_order = _make_signed_order() if action == ExecutionAction.EXECUTED else None
    return ExecutionResult(
        action=action,
        reason=None,
        order_payload=_make_order_data(OrderSide.BUY),
        signed_order=signed_order,
        kelly_fraction=Decimal("0.03"),
        order_size_usdc=Decimal("12.5"),
        midpoint_probability=Decimal("0.55"),
        best_ask=Decimal("0.56"),
        bankroll_usdc=Decimal("1000"),
    )


def _make_exit_result(*, position_id: str) -> ExitResult:
    return ExitResult(
        position_id=position_id,
        condition_id=f"condition-{position_id}",
        should_exit=True,
        exit_reason=ExitReason.STOP_LOSS,
        entry_price=Decimal("0.60"),
        current_midpoint=Decimal("0.55"),
        current_best_bid=Decimal("0.54"),
        position_age_hours=Decimal("8"),
        unrealized_edge=Decimal("-0.05"),
        evaluated_at_utc=datetime.now(timezone.utc),
    )


def _make_position_record(*, position_id: str, token_id: str) -> PositionRecord:
    now = datetime.now(timezone.utc)
    return PositionRecord(
        id=position_id,
        condition_id=f"condition-{position_id}",
        token_id=token_id,
        status=PositionStatus.OPEN,
        side="BUY",
        entry_price=Decimal("0.60"),
        order_size_usdc=Decimal("30"),
        kelly_fraction=Decimal("0.10"),
        best_ask_at_entry=Decimal("0.61"),
        bankroll_usdc_at_entry=Decimal("1000"),
        execution_action=ExecutionAction.EXECUTED,
        reason=None,
        routed_at_utc=now,
        recorded_at_utc=now,
    )


def _make_exit_order_result(*, position_id: str, action: ExitOrderAction) -> ExitOrderResult:
    signed_order = _make_signed_order() if action == ExitOrderAction.SELL_ROUTED else None
    return ExitOrderResult(
        position_id=position_id,
        condition_id=f"condition-{position_id}",
        action=action,
        reason=None,
        order_payload=_make_order_data(OrderSide.SELL),
        signed_order=signed_order,
        exit_price=Decimal("0.55"),
        order_size_usdc=Decimal("30"),
    )


def _make_order_data(side: OrderSide) -> OrderData:
    return OrderData(
        salt=1,
        maker=WALLET_ADDRESS,
        signer=WALLET_ADDRESS,
        taker="0x0000000000000000000000000000000000000000",
        token_id=123,
        maker_amount=50_000_000,
        taker_amount=27_500_000,
        expiration=0,
        nonce=0,
        fee_rate_bps=0,
        side=side,
        signature_type=SIGNATURE_TYPE_EOA,
    )


def _make_signed_order() -> SignedOrder:
    return SignedOrder(
        order=_make_order_data(OrderSide.BUY),
        signature="0x" + "ab" * 65,
        owner=WALLET_ADDRESS,
    )


def _make_notifier_config(*, bot_token: str = "token-123", chat_id: str = "chat-123"):
    return SimpleNamespace(
        telegram_bot_token=SecretStr(bot_token),
        telegram_chat_id=chat_id,
        telegram_send_timeout_sec=Decimal("0.01"),
    )


def _make_failing_http_client():
    request = httpx.Request(
        "POST",
        "https://api.telegram.org/bottoken-123/sendMessage",
    )
    client = MagicMock()
    client.post = AsyncMock(
        side_effect=httpx.TimeoutException("telegram timeout", request=request)
    )
    return client


def test_telegram_notifier_module_has_no_forbidden_imports():
    if not TELEGRAM_MODULE_PATH.exists():
        pytest.fail(
            "Expected Telegram notifier implementation file at "
            "src/agents/execution/telegram_notifier.py.",
            pytrace=False,
        )

    tree = ast.parse(TELEGRAM_MODULE_PATH.read_text())
    imported: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)

    forbidden_prefix_matches = sorted(
        module_name
        for module_name in imported
        if module_name.startswith(FORBIDDEN_IMPORT_PREFIXES)
    )
    forbidden_exact_matches = sorted(
        module_name for module_name in imported if module_name in FORBIDDEN_IMPORTS
    )

    assert forbidden_prefix_matches == []
    assert forbidden_exact_matches == []


@pytest.mark.asyncio
async def test_orchestrator_sets_telegram_notifier_none_when_feature_disabled(
    test_config,
):
    object.__setattr__(test_config, "enable_telegram_notifier", False)
    object.__setattr__(test_config, "telegram_bot_token", SecretStr("token-123"))
    object.__setattr__(test_config, "telegram_chat_id", "chat-123")

    orchestrator = _build_orchestrator(test_config)

    assert hasattr(orchestrator, "telegram_notifier")
    assert orchestrator.telegram_notifier is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("bot_token", "chat_id"),
    [
        ("", "chat-123"),
        ("token-123", ""),
    ],
)
async def test_orchestrator_sets_telegram_notifier_none_when_credentials_missing(
    test_config,
    bot_token: str,
    chat_id: str,
):
    object.__setattr__(test_config, "enable_telegram_notifier", True)
    object.__setattr__(test_config, "telegram_bot_token", SecretStr(bot_token))
    object.__setattr__(test_config, "telegram_chat_id", chat_id)

    orchestrator = _build_orchestrator(test_config)

    assert orchestrator.telegram_notifier is None


@pytest.mark.asyncio
async def test_orchestrator_constructs_telegram_notifier_when_all_config_gates_satisfied(
    test_config,
):
    module = _load_module(TELEGRAM_MODULE_NAME)
    telegram_http_client = MagicMock()

    object.__setattr__(test_config, "enable_telegram_notifier", True)
    object.__setattr__(test_config, "telegram_bot_token", SecretStr("token-123"))
    object.__setattr__(test_config, "telegram_chat_id", "chat-123")
    object.__setattr__(test_config, "telegram_send_timeout_sec", Decimal("5"))

    orchestrator = _build_orchestrator(
        test_config,
        telegram_http_client=telegram_http_client,
    )

    assert isinstance(orchestrator.telegram_notifier, module.TelegramNotifier)
    assert orchestrator._telegram_client is telegram_http_client


@pytest.mark.asyncio
async def test_portfolio_aggregation_loop_sends_each_fired_alert_to_telegram(
    monkeypatch,
    test_config,
):
    orchestrator = _build_orchestrator(test_config)
    object.__setattr__(
        orchestrator.config,
        "portfolio_aggregation_interval_sec",
        Decimal("0.01"),
    )

    alerts = [
        _make_alert(severity=AlertSeverity.CRITICAL, rule_name="drawdown"),
        _make_alert(
            severity=AlertSeverity.WARNING,
            rule_name="loss_rate",
            message="Loss rate threshold exceeded",
        ),
    ]

    sleep_calls = 0

    async def _fake_sleep(seconds: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= 2:
            raise asyncio.CancelledError

    orchestrator.portfolio_aggregator.compute_snapshot = AsyncMock(
        return_value=_make_snapshot(dry_run=True)
    )
    orchestrator.lifecycle_reporter.generate_report = AsyncMock(
        return_value=_make_report(dry_run=True)
    )
    orchestrator.alert_engine = MagicMock()
    orchestrator.alert_engine.evaluate = MagicMock(return_value=alerts)
    orchestrator.telegram_notifier = MagicMock()
    orchestrator.telegram_notifier.send_alert = AsyncMock(return_value=None)

    monkeypatch.setattr("src.orchestrator.asyncio.sleep", _fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await orchestrator._portfolio_aggregation_loop()

    assert orchestrator.telegram_notifier.send_alert.await_count == 2
    orchestrator.telegram_notifier.send_alert.assert_any_await(alerts[0])
    orchestrator.telegram_notifier.send_alert.assert_any_await(alerts[1])


@pytest.mark.asyncio
async def test_portfolio_aggregation_loop_continues_after_telegram_send_failure(
    monkeypatch,
    test_config,
):
    module = _load_module(TELEGRAM_MODULE_NAME)
    failing_client = _make_failing_http_client()
    orchestrator = _build_orchestrator(test_config)
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
        return_value=_make_snapshot(dry_run=True)
    )
    orchestrator.lifecycle_reporter.generate_report = AsyncMock(
        return_value=_make_report(dry_run=True)
    )
    orchestrator.alert_engine = MagicMock()
    orchestrator.alert_engine.evaluate = MagicMock(return_value=[_make_alert()])
    orchestrator.telegram_notifier = module.TelegramNotifier(
        _make_notifier_config(),
        failing_client,
    )

    monkeypatch.setattr("src.orchestrator.asyncio.sleep", _fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await orchestrator._portfolio_aggregation_loop()

    assert failing_client.post.await_count == 2
    assert orchestrator.alert_engine.evaluate.call_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("dry_run", "action"),
    [
        (True, ExecutionAction.DRY_RUN),
        (False, ExecutionAction.EXECUTED),
    ],
)
async def test_execution_consumer_loop_sends_buy_routed_execution_event(
    test_config,
    dry_run: bool,
    action: ExecutionAction,
):
    orchestrator = _build_orchestrator(test_config)
    object.__setattr__(orchestrator.config, "dry_run", dry_run)

    fake_eval = MagicMock()
    fake_eval.market_context.condition_id = "condition-buy-001"
    fake_eval.recommended_action.value = "BUY"

    execution_result = _make_execution_result(action=action)
    orchestrator.broadcaster = AsyncMock()
    orchestrator.broadcaster.broadcast = AsyncMock()
    orchestrator.execution_router.route = AsyncMock(return_value=execution_result)
    orchestrator.position_tracker.record_execution = AsyncMock(return_value=None)
    orchestrator.telegram_notifier = MagicMock()
    orchestrator.telegram_notifier.send_execution_event = AsyncMock(return_value=None)

    await orchestrator.execution_queue.put(
        {
            "snapshot_id": "snap-001",
            "evaluation": fake_eval,
            "yes_token_id": "tok-yes-001",
        }
    )

    consumer_task = asyncio.create_task(orchestrator._execution_consumer_loop())
    await asyncio.sleep(0.05)
    consumer_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer_task

    orchestrator.telegram_notifier.send_execution_event.assert_awaited_once()
    call = orchestrator.telegram_notifier.send_execution_event.await_args
    assert "BUY ROUTED" in call.kwargs["summary"]
    assert "condition-buy-001" in call.kwargs["summary"]
    assert action.value in call.kwargs["summary"]
    assert call.kwargs["dry_run"] is dry_run


@pytest.mark.asyncio
async def test_execution_consumer_loop_continues_after_telegram_send_failure(
    test_config,
):
    module = _load_module(TELEGRAM_MODULE_NAME)
    orchestrator = _build_orchestrator(test_config)
    failing_client = _make_failing_http_client()

    object.__setattr__(orchestrator.config, "dry_run", True)
    orchestrator.broadcaster = AsyncMock()
    orchestrator.broadcaster.broadcast = AsyncMock()
    orchestrator.execution_router.route = AsyncMock(
        return_value=_make_execution_result(action=ExecutionAction.DRY_RUN)
    )
    orchestrator.position_tracker.record_execution = AsyncMock(return_value=None)
    orchestrator.telegram_notifier = module.TelegramNotifier(
        _make_notifier_config(),
        failing_client,
    )

    for idx in (1, 2):
        fake_eval = MagicMock()
        fake_eval.market_context.condition_id = f"condition-buy-{idx}"
        fake_eval.recommended_action.value = "BUY"
        await orchestrator.execution_queue.put(
            {
                "snapshot_id": f"snap-00{idx}",
                "evaluation": fake_eval,
                "yes_token_id": f"tok-yes-00{idx}",
            }
        )

    consumer_task = asyncio.create_task(orchestrator._execution_consumer_loop())
    await asyncio.sleep(0.1)
    consumer_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer_task

    assert orchestrator.execution_router.route.await_count == 2
    assert failing_client.post.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("dry_run", "action"),
    [
        (True, ExitOrderAction.DRY_RUN),
        (False, ExitOrderAction.SELL_ROUTED),
    ],
)
async def test_exit_scan_loop_sends_sell_routed_execution_event(
    monkeypatch,
    test_config,
    dry_run: bool,
    action: ExitOrderAction,
):
    orchestrator = _build_orchestrator(test_config)
    object.__setattr__(orchestrator.config, "dry_run", dry_run)
    object.__setattr__(orchestrator.config, "exit_scan_interval_seconds", Decimal("0.01"))

    sleep_calls = 0

    async def _fake_sleep(seconds: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= 2:
            raise asyncio.CancelledError

    exit_result = _make_exit_result(position_id="pos-001")
    position = _make_position_record(position_id="pos-001", token_id="tok-001")
    exit_order_result = _make_exit_order_result(position_id="pos-001", action=action)

    orchestrator.exit_strategy_engine.scan_open_positions = AsyncMock(
        return_value=[exit_result]
    )
    orchestrator._fetch_position_record = AsyncMock(return_value=position)
    orchestrator.exit_order_router.route_exit = AsyncMock(return_value=exit_order_result)
    orchestrator.pnl_calculator.settle = AsyncMock(return_value=MagicMock())
    orchestrator.broadcaster = AsyncMock()
    orchestrator.broadcaster.broadcast = AsyncMock()
    orchestrator.telegram_notifier = MagicMock()
    orchestrator.telegram_notifier.send_execution_event = AsyncMock(return_value=None)

    monkeypatch.setattr("src.orchestrator.asyncio.sleep", _fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await orchestrator._exit_scan_loop()

    orchestrator.telegram_notifier.send_execution_event.assert_awaited_once()
    call = orchestrator.telegram_notifier.send_execution_event.await_args
    assert "SELL ROUTED" in call.kwargs["summary"]
    assert "pos-001" in call.kwargs["summary"]
    assert action.value in call.kwargs["summary"]
    assert call.kwargs["dry_run"] is dry_run


@pytest.mark.asyncio
async def test_exit_scan_loop_continues_evaluating_positions_after_telegram_send_failure(
    monkeypatch,
    test_config,
):
    module = _load_module(TELEGRAM_MODULE_NAME)
    orchestrator = _build_orchestrator(test_config)
    failing_client = _make_failing_http_client()

    object.__setattr__(orchestrator.config, "dry_run", True)
    object.__setattr__(orchestrator.config, "exit_scan_interval_seconds", Decimal("0.01"))

    sleep_calls = 0

    async def _fake_sleep(seconds: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= 2:
            raise asyncio.CancelledError

    orchestrator.exit_strategy_engine.scan_open_positions = AsyncMock(
        return_value=[
            _make_exit_result(position_id="pos-101"),
            _make_exit_result(position_id="pos-102"),
        ]
    )
    orchestrator._fetch_position_record = AsyncMock(
        side_effect=[
            _make_position_record(position_id="pos-101", token_id="tok-101"),
            _make_position_record(position_id="pos-102", token_id="tok-102"),
        ]
    )
    orchestrator.exit_order_router.route_exit = AsyncMock(
        side_effect=[
            _make_exit_order_result(
                position_id="pos-101",
                action=ExitOrderAction.DRY_RUN,
            ),
            _make_exit_order_result(
                position_id="pos-102",
                action=ExitOrderAction.DRY_RUN,
            ),
        ]
    )
    orchestrator.pnl_calculator.settle = AsyncMock(return_value=MagicMock())
    orchestrator.telegram_notifier = module.TelegramNotifier(
        _make_notifier_config(),
        failing_client,
    )

    monkeypatch.setattr("src.orchestrator.asyncio.sleep", _fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await orchestrator._exit_scan_loop()

    assert orchestrator.exit_order_router.route_exit.await_count == 2
    assert failing_client.post.await_count == 2


@pytest.mark.asyncio
async def test_shutdown_closes_telegram_client_and_clears_reference(test_config):
    patches = _patch_heavy_deps()
    mock_engine = patches["engine"]

    with patch.multiple("src.orchestrator", **patches):
        orchestrator = Orchestrator(test_config)

    telegram_client = MagicMock()
    telegram_client.aclose = AsyncMock()
    httpx_client = MagicMock()
    httpx_client.aclose = AsyncMock()
    http_session = MagicMock()
    http_session.close = AsyncMock()

    orchestrator._telegram_client = telegram_client
    orchestrator._httpx_client = httpx_client
    orchestrator._http_session = http_session
    orchestrator.aggregator = AsyncMock()
    orchestrator.aggregator.stop = AsyncMock()
    orchestrator.claude_client.stop = AsyncMock()

    with patch.multiple("src.orchestrator", engine=mock_engine):
        await orchestrator.shutdown()

    httpx_client.aclose.assert_awaited_once()
    http_session.close.assert_awaited_once()
    telegram_client.aclose.assert_awaited_once()
    assert orchestrator._telegram_client is None
