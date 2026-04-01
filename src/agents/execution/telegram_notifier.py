"""
src/agents/execution/telegram_notifier.py

WI-26 Telegram notification sink.
"""

from __future__ import annotations

import httpx
import structlog

from src.core.config import AppConfig
from src.schemas.risk import AlertEvent, AlertSeverity

_SEVERITY_EMOJI: dict[AlertSeverity, str] = {
    AlertSeverity.CRITICAL: "🚨",
    AlertSeverity.WARNING: "⚠️",
    AlertSeverity.INFO: "ℹ️",
}


class TelegramNotifier:
    """Best-effort Telegram Bot API notifier for alerts and execution events."""

    def __init__(
        self,
        config: AppConfig,
        http_client: httpx.AsyncClient,
    ) -> None:
        self._bot_token = config.telegram_bot_token.get_secret_value()
        self._chat_id = config.telegram_chat_id
        self._timeout = float(config.telegram_send_timeout_sec)
        self._client = http_client
        self._log = structlog.get_logger(__name__)

    async def send_alert(self, alert: AlertEvent) -> None:
        """Format and send a structured alert event."""
        emoji = _SEVERITY_EMOJI[alert.severity]
        text = (
            f"{emoji} ALERT: {alert.rule_name}\n\n"
            f"{alert.message}\n\n"
            f"Threshold: {alert.threshold_value}\n"
            f"Actual: {alert.actual_value}\n"
            f"Time: {alert.alert_at_utc.isoformat()}"
        )
        if alert.dry_run:
            text = f"[DRY RUN] {text}"

        sent = await self._send(text)
        if sent:
            self._log.info(
                "telegram.message_sent",
                rule_name=alert.rule_name,
                severity=alert.severity.value,
            )

    async def send_execution_event(self, summary: str, dry_run: bool) -> None:
        """Send a free-form execution summary."""
        text = f"[DRY RUN] {summary}" if dry_run else summary
        sent = await self._send(text)
        if sent:
            self._log.info("telegram.message_sent", event_type="execution")

    async def _send(self, text: str) -> None:
        """Send a single Telegram message and swallow all failures."""
        url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "HTML",
        }
        try:
            response = await self._client.post(
                url,
                json=payload,
                timeout=self._timeout,
            )
            response.raise_for_status()
            return True
        except Exception as exc:
            self._log.error("telegram.send_failed", error=str(exc))
            return False
