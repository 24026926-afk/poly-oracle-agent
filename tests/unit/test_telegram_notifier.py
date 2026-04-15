"""
tests/unit/test_telegram_notifier.py

RED-phase unit tests for WI-26 TelegramNotifier.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import importlib
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from pydantic import SecretStr

from src.core.config import AppConfig
from src.schemas.risk import AlertEvent, AlertSeverity


TELEGRAM_MODULE_NAME = "src.agents.execution.telegram_notifier"


def _load_module(name: str):
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError:
        pytest.fail(f"Expected module {name} to exist.", pytrace=False)
    except Exception as exc:
        pytest.fail(f"Module {name} import failed unexpectedly: {exc!r}", pytrace=False)


def _make_config(
    *,
    bot_token: str = "telegram-secret-token",
    chat_id: str = "chat-123",
    timeout: Decimal = Decimal("5"),
):
    return SimpleNamespace(
        telegram_bot_token=SecretStr(bot_token),
        telegram_chat_id=chat_id,
        telegram_send_timeout_sec=timeout,
    )


def _make_alert(
    *,
    severity: AlertSeverity,
    dry_run: bool,
    rule_name: str = "drawdown",
    threshold_value: Decimal = Decimal("100"),
    actual_value: Decimal = Decimal("-142.50"),
) -> AlertEvent:
    return AlertEvent(
        alert_at_utc=datetime(2026, 4, 1, 14, 30, tzinfo=timezone.utc),
        severity=severity,
        rule_name=rule_name,
        message=("Portfolio drawdown exceeds 100 USDC: unrealized PnL is -142.50 USDC"),
        threshold_value=threshold_value,
        actual_value=actual_value,
        dry_run=dry_run,
    )


def _make_success_client():
    response = MagicMock()
    response.raise_for_status = MagicMock(return_value=None)

    client = MagicMock()
    client.post = AsyncMock(return_value=response)
    return client


def _make_http_status_client(status_code: int):
    request = httpx.Request(
        "POST",
        "https://api.telegram.org/bottelegram-secret-token/sendMessage",
    )
    response = httpx.Response(status_code=status_code, request=request)

    client = MagicMock()
    client.post = AsyncMock(return_value=response)
    return client


def test_telegram_notifier_contract_exists_with_expected_public_async_methods():
    module = _load_module(TELEGRAM_MODULE_NAME)
    notifier_cls = getattr(module, "TelegramNotifier", None)

    assert notifier_cls is not None
    assert inspect.isclass(notifier_cls)
    assert list(inspect.signature(notifier_cls.__init__).parameters.keys()) == [
        "self",
        "config",
        "http_client",
    ]
    assert inspect.iscoroutinefunction(notifier_cls.send_alert)
    assert inspect.iscoroutinefunction(notifier_cls.send_execution_event)
    assert inspect.iscoroutinefunction(notifier_cls._send)


def test_app_config_includes_telegram_fields_with_expected_defaults():
    fields = AppConfig.model_fields

    assert "enable_telegram_notifier" in fields
    assert fields["enable_telegram_notifier"].annotation is bool
    assert fields["enable_telegram_notifier"].default is False

    assert "telegram_bot_token" in fields
    assert fields["telegram_bot_token"].annotation is SecretStr
    assert isinstance(fields["telegram_bot_token"].default, SecretStr)
    assert fields["telegram_bot_token"].default.get_secret_value() == ""

    assert "telegram_chat_id" in fields
    assert fields["telegram_chat_id"].annotation is str
    assert fields["telegram_chat_id"].default == ""

    assert "telegram_send_timeout_sec" in fields
    assert fields["telegram_send_timeout_sec"].annotation is Decimal
    assert fields["telegram_send_timeout_sec"].default == Decimal("5")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("severity", "emoji"),
    [
        (AlertSeverity.CRITICAL, "🚨"),
        (AlertSeverity.WARNING, "⚠️"),
        (AlertSeverity.INFO, "ℹ️"),
    ],
)
async def test_send_alert_formats_expected_alert_message(severity, emoji):
    module = _load_module(TELEGRAM_MODULE_NAME)
    client = _make_success_client()
    notifier = module.TelegramNotifier(_make_config(), client)

    alert = _make_alert(severity=severity, dry_run=False)

    await notifier.send_alert(alert)

    post_call = client.post.await_args
    assert post_call is not None
    assert post_call.kwargs["json"]["text"] == (
        f"{emoji} ALERT: drawdown\n\n"
        "Portfolio drawdown exceeds 100 USDC: unrealized PnL is -142.50 USDC\n\n"
        "Threshold: 100\n"
        "Actual: -142.50\n"
        "Time: 2026-04-01T14:30:00+00:00"
    )


@pytest.mark.asyncio
async def test_send_alert_prefixes_dry_run_messages():
    module = _load_module(TELEGRAM_MODULE_NAME)
    client = _make_success_client()
    notifier = module.TelegramNotifier(_make_config(), client)

    await notifier.send_alert(
        _make_alert(severity=AlertSeverity.CRITICAL, dry_run=True)
    )

    text = client.post.await_args.kwargs["json"]["text"]
    assert text.startswith("[DRY RUN] ")


@pytest.mark.asyncio
async def test_send_alert_omits_dry_run_prefix_when_false():
    module = _load_module(TELEGRAM_MODULE_NAME)
    client = _make_success_client()
    notifier = module.TelegramNotifier(_make_config(), client)

    await notifier.send_alert(
        _make_alert(severity=AlertSeverity.CRITICAL, dry_run=False)
    )

    text = client.post.await_args.kwargs["json"]["text"]
    assert "[DRY RUN]" not in text


@pytest.mark.asyncio
async def test_send_execution_event_prefixes_dry_run_messages():
    module = _load_module(TELEGRAM_MODULE_NAME)
    client = _make_success_client()
    notifier = module.TelegramNotifier(_make_config(), client)

    await notifier.send_execution_event(
        "BUY ROUTED: condition-001 | 25 USDC | action=DRY_RUN",
        dry_run=True,
    )

    assert client.post.await_args.kwargs["json"]["text"].startswith("[DRY RUN] ")


@pytest.mark.asyncio
async def test_send_execution_event_passes_summary_through_when_live():
    module = _load_module(TELEGRAM_MODULE_NAME)
    client = _make_success_client()
    notifier = module.TelegramNotifier(_make_config(), client)
    summary = "SELL ROUTED: pos-001 | exit_price=0.55 | action=SELL_ROUTED"

    await notifier.send_execution_event(summary, dry_run=False)

    assert client.post.await_args.kwargs["json"]["text"] == summary


@pytest.mark.asyncio
async def test_send_uses_expected_telegram_api_url_payload_and_timeout():
    module = _load_module(TELEGRAM_MODULE_NAME)
    client = _make_success_client()
    notifier = module.TelegramNotifier(
        _make_config(
            bot_token="bot-token-xyz",
            chat_id="chat-999",
            timeout=Decimal("7.5"),
        ),
        client,
    )

    await notifier.send_execution_event(
        "BUY ROUTED: condition-xyz | 12 USDC | action=EXECUTED",
        dry_run=False,
    )

    call = client.post.await_args
    assert call.args[0] == "https://api.telegram.org/botbot-token-xyz/sendMessage"
    assert call.kwargs["json"] == {
        "chat_id": "chat-999",
        "text": "BUY ROUTED: condition-xyz | 12 USDC | action=EXECUTED",
        "parse_mode": "HTML",
    }
    assert call.kwargs["timeout"] == 7.5


@pytest.mark.asyncio
async def test_send_alert_swallows_timeout_exception():
    module = _load_module(TELEGRAM_MODULE_NAME)
    client = MagicMock()
    client.post = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
    notifier = module.TelegramNotifier(_make_config(), client)

    await notifier.send_alert(
        _make_alert(severity=AlertSeverity.CRITICAL, dry_run=False)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [403, 500])
async def test_send_alert_swallows_http_status_errors(status_code: int):
    module = _load_module(TELEGRAM_MODULE_NAME)
    notifier = module.TelegramNotifier(
        _make_config(), _make_http_status_client(status_code)
    )

    await notifier.send_alert(
        _make_alert(severity=AlertSeverity.CRITICAL, dry_run=False)
    )


@pytest.mark.asyncio
async def test_send_alert_swallows_connect_error():
    module = _load_module(TELEGRAM_MODULE_NAME)
    request = httpx.Request(
        "POST",
        "https://api.telegram.org/bottelegram-secret-token/sendMessage",
    )
    client = MagicMock()
    client.post = AsyncMock(
        side_effect=httpx.ConnectError("connect boom", request=request)
    )
    notifier = module.TelegramNotifier(_make_config(), client)

    await notifier.send_alert(
        _make_alert(severity=AlertSeverity.CRITICAL, dry_run=False)
    )


@pytest.mark.asyncio
async def test_send_alert_swallows_generic_runtime_error():
    module = _load_module(TELEGRAM_MODULE_NAME)
    client = MagicMock()
    client.post = AsyncMock(side_effect=RuntimeError("unexpected-boom"))
    notifier = module.TelegramNotifier(_make_config(), client)

    await notifier.send_alert(
        _make_alert(severity=AlertSeverity.CRITICAL, dry_run=False)
    )


@pytest.mark.asyncio
async def test_send_execution_event_swallows_timeout_exception():
    module = _load_module(TELEGRAM_MODULE_NAME)
    client = MagicMock()
    client.post = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
    notifier = module.TelegramNotifier(_make_config(), client)

    await notifier.send_execution_event(
        "BUY ROUTED: condition-001 | 25 USDC | action=DRY_RUN",
        dry_run=True,
    )


@pytest.mark.asyncio
async def test_failed_send_does_not_log_raw_bot_token():
    module = _load_module(TELEGRAM_MODULE_NAME)
    raw_bot_token = "super-secret-bot-token"
    client = MagicMock()
    client.post = AsyncMock(side_effect=RuntimeError("telegram exploded"))
    notifier = module.TelegramNotifier(
        _make_config(bot_token=raw_bot_token),
        client,
    )
    notifier._log = MagicMock()

    await notifier.send_alert(
        _make_alert(severity=AlertSeverity.CRITICAL, dry_run=False)
    )

    rendered = " ".join(
        [
            repr(call)
            for call in notifier._log.error.call_args_list
            + notifier._log.info.call_args_list
        ]
    )
    assert raw_bot_token not in rendered
