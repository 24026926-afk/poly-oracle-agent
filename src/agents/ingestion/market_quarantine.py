"""
src/agents/ingestion/market_quarantine.py

In-memory quarantine for markets that repeatedly fail preflight checks.

A market that fails preflight N times within a window is placed in
quarantine for a configurable duration.  Quarantine is per-market and
does not suppress unrelated markets.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, Optional

import structlog

from src.core.config import AppConfig
from src.schemas.market_eligibility import (
    MarketQuarantineDecision,
    MarketQuarantineReason,
)

logger = structlog.get_logger(__name__)


class MarketQuarantineManager:
    """Tracks per-market quarantine state in memory.

    A market is quarantined when it accumulates >= failure_threshold
    consecutive preflight failures.  The quarantine expires after
    the configured duration, at which point the market may be
    re-evaluated.
    """

    def __init__(self, config: AppConfig, failure_threshold: int = 3) -> None:
        self._config = config
        self._failure_threshold = failure_threshold
        # condition_id -> consecutive failure count
        self._failure_counts: Dict[str, int] = {}
        # condition_id -> quarantine decision (active quarantine)
        self._quarantined: Dict[str, MarketQuarantineDecision] = {}

    def is_quarantined(self, condition_id: str) -> bool:
        """Return True if the market is currently in quarantine."""
        decision = self._quarantined.get(condition_id)
        if decision is None:
            return False
        if datetime.now(timezone.utc) >= decision.expires_at_utc:
            # Quarantine expired — clean up
            del self._quarantined[condition_id]
            self._failure_counts.pop(condition_id, None)
            return False
        return True

    def record_failure(self, condition_id: str) -> Optional[MarketQuarantineDecision]:
        """Record a preflight failure.  Returns a quarantine decision if threshold reached."""
        if self.is_quarantined(condition_id):
            return None  # Already quarantined

        self._failure_counts[condition_id] = (
            self._failure_counts.get(condition_id, 0) + 1
        )

        if self._failure_counts[condition_id] >= self._failure_threshold:
            now = datetime.now(timezone.utc)
            duration = self._config.preflight_quarantine_duration_seconds
            expires = now + timedelta_from_decimal(duration)

            decision = MarketQuarantineDecision(
                condition_id=condition_id,
                reason=MarketQuarantineReason.REPEATED_PREFLIGHT_FAILURE,
                quarantined_at_utc=now,
                expires_at_utc=expires,
            )
            self._quarantined[condition_id] = decision
            logger.warning(
                "quarantine.market_quarantined",
                reason=MarketQuarantineReason.REPEATED_PREFLIGHT_FAILURE.value,
            )
            return decision

        return None

    def record_success(self, condition_id: str) -> None:
        """Record a successful preflight — reset failure counter."""
        self._failure_counts.pop(condition_id, None)
        # If quarantined, remove from quarantine on success
        self._quarantined.pop(condition_id, None)

    def get_quarantine_decision(
        self, condition_id: str
    ) -> Optional[MarketQuarantineDecision]:
        """Return the active quarantine decision for a market, or None."""
        if self.is_quarantined(condition_id):
            return self._quarantined.get(condition_id)
        return None

    @property
    def quarantined_count(self) -> int:
        """Number of currently quarantined markets."""
        # Count only non-expired quarantines
        now = datetime.now(timezone.utc)
        return sum(1 for d in self._quarantined.values() if d.expires_at_utc > now)

    def clear(self) -> None:
        """Clear all quarantine state (e.g. on restart)."""
        self._failure_counts.clear()
        self._quarantined.clear()


def timedelta_from_decimal(seconds: Decimal) -> object:
    """Convert a Decimal number of seconds to a timedelta."""
    return __import__("datetime").timedelta(seconds=float(seconds))
