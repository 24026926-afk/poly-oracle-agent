"""
src/agents/execution/alert_engine.py

WI-25 stateless, read-only alert evaluation engine.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from src.core.config import AppConfig
from src.schemas.risk import AlertEvent, AlertSeverity, LifecycleReport, PortfolioSnapshot

_ZERO = Decimal("0")


class AlertEngine:
    """Evaluate portfolio and lifecycle metrics against alert thresholds."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config

    def evaluate(
        self,
        snapshot: PortfolioSnapshot,
        report: LifecycleReport,
    ) -> list[AlertEvent]:
        """Evaluate all alert rules in deterministic order."""
        alerts: list[AlertEvent] = []
        now = datetime.now(timezone.utc)
        dry_run = snapshot.dry_run

        neg_threshold = _ZERO - self._config.alert_drawdown_usdc
        if snapshot.total_unrealized_pnl < neg_threshold:
            alerts.append(
                AlertEvent(
                    alert_at_utc=now,
                    severity=AlertSeverity.CRITICAL,
                    rule_name="drawdown",
                    message=(
                        f"Portfolio drawdown exceeds "
                        f"{self._config.alert_drawdown_usdc} USDC: "
                        f"unrealized PnL is {snapshot.total_unrealized_pnl} USDC"
                    ),
                    threshold_value=self._config.alert_drawdown_usdc,
                    actual_value=snapshot.total_unrealized_pnl,
                    dry_run=dry_run,
                )
            )

        if snapshot.position_count > 0:
            stale_ratio = Decimal(str(snapshot.positions_with_stale_price)) / Decimal(
                str(snapshot.position_count)
            )
            if stale_ratio > self._config.alert_stale_price_pct:
                alerts.append(
                    AlertEvent(
                        alert_at_utc=now,
                        severity=AlertSeverity.WARNING,
                        rule_name="stale_price",
                        message=(
                            f"Stale price ratio {stale_ratio} exceeds "
                            f"threshold {self._config.alert_stale_price_pct}"
                        ),
                        threshold_value=self._config.alert_stale_price_pct,
                        actual_value=stale_ratio,
                        dry_run=dry_run,
                    )
                )

        if snapshot.position_count > self._config.alert_max_open_positions:
            alerts.append(
                AlertEvent(
                    alert_at_utc=now,
                    severity=AlertSeverity.WARNING,
                    rule_name="max_positions",
                    message=(
                        f"Open position count {snapshot.position_count} "
                        f"exceeds limit {self._config.alert_max_open_positions}"
                    ),
                    threshold_value=Decimal(str(self._config.alert_max_open_positions)),
                    actual_value=Decimal(str(snapshot.position_count)),
                    dry_run=dry_run,
                )
            )

        if report.total_settled_count > 0:
            loss_rate = Decimal(str(report.losing_count)) / Decimal(
                str(report.total_settled_count)
            )
            if loss_rate > self._config.alert_loss_rate_pct:
                alerts.append(
                    AlertEvent(
                        alert_at_utc=now,
                        severity=AlertSeverity.WARNING,
                        rule_name="loss_rate",
                        message=(
                            f"Loss rate {loss_rate} exceeds "
                            f"threshold {self._config.alert_loss_rate_pct}"
                        ),
                        threshold_value=self._config.alert_loss_rate_pct,
                        actual_value=loss_rate,
                        dry_run=dry_run,
                    )
                )

        return alerts
