"""
src/agents/evaluation/llm_cost_guard.py

WI-52: LLM Cost Guard and Cognitive Circuit Breaker.

Provides:
- ``LLMBudgetGuard`` — enforces hourly/daily call, token, and cost limits
  before any paid LLM provider call.
- ``MarketCognitiveCircuitBreaker`` — tracks repeated non-actionable or
  invalid outcomes per market and enforces bounded cooldowns.

All cost/token math uses ``Decimal``.  Raw ``float`` is forbidden in
money paths.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Any, Dict, Optional

import structlog

from src.core.config import AppConfig
from src.schemas.llm import (
    LLMProviderName,
    LLMUsageRecord,
    LLMBudgetWindow,
    LLMBudgetDecision,
    LLMBudgetBlockReason,
    MarketCognitiveState,
    MarketCooldownDecision,
    MarketCooldownReason,
    LLMCostGuardSnapshot,
)

logger = structlog.get_logger(__name__)


def _hash_market_key(market_key: str) -> str:
    """Hash a market key to a short identifier for logs/metrics.

    Prevents raw condition_id exposure per WI-52 low-cardinality requirement."""
    return hashlib.sha256(market_key.encode()).hexdigest()[:8]


# Type alias to avoid circular import with MetricsRegistry at module level
_MetricsRegistry = Any

# ---------------------------------------------------------------------------
# LLMBudgetGuard
# ---------------------------------------------------------------------------


class LLMBudgetGuard:
    """Thread-safe (asyncio.Lock) budget enforcement gate.

    Must be called *before* any primary evaluation or reflection LLM call.
    Returns an ``LLMBudgetDecision`` — if ``allowed=False`` the caller
    must NOT make the provider call.
    """

    def __init__(
        self, config: AppConfig, metrics: Optional[_MetricsRegistry] = None
    ) -> None:
        self._config = config
        self._metrics = metrics
        self._lock = asyncio.Lock()
        self._global_window = LLMBudgetWindow()
        self._per_market_hourly: Dict[str, int] = {}
        self._per_market_hourly_reset: Dict[str, datetime] = {}
        self._total_tokens_consumed: int = 0
        self._total_cost_usd: Decimal = Decimal("0")
        self._daily_window_start: Optional[datetime] = None
        self._hourly_window_start: Optional[datetime] = None
        self._estimated_spend_usd: Decimal = Decimal("0")

    # -- Public API -------------------------------------------------------

    async def check_budget(
        self,
        *,
        call_type: str = "primary",
        market_key: Optional[str] = None,
    ) -> LLMBudgetDecision:
        """Return allow/block decision.  Caller must NOT call LLM if blocked.

        WI-52: Zero limits mean NO calls allowed when guard is enabled.
        WI-52: Check and counter reservation are atomic under the lock.
        """
        if not self._config.enable_llm_cost_guard:
            return LLMBudgetDecision(allowed=True, call_type=call_type)

        async with self._lock:
            self._refresh_windows()

            # WI-52: Zero limits = no calls allowed (fail closed)
            hourly_limit = self._config.llm_hourly_call_limit
            if hourly_limit == 0:
                return self._block(
                    LLMBudgetBlockReason.HOURLY_CALL_LIMIT_EXHAUSTED, call_type
                )
            if self._global_window.hourly_calls >= hourly_limit:
                return self._block(
                    LLMBudgetBlockReason.HOURLY_CALL_LIMIT_EXHAUSTED, call_type
                )

            daily_limit = self._config.llm_daily_call_limit
            if daily_limit == 0:
                return self._block(
                    LLMBudgetBlockReason.DAILY_CALL_LIMIT_EXHAUSTED, call_type
                )
            if self._global_window.daily_calls >= daily_limit:
                return self._block(
                    LLMBudgetBlockReason.DAILY_CALL_LIMIT_EXHAUSTED, call_type
                )

            token_limit = self._config.llm_daily_token_limit
            if token_limit == 0:
                return self._block(
                    LLMBudgetBlockReason.DAILY_TOKEN_LIMIT_EXHAUSTED, call_type
                )
            if self._total_tokens_consumed >= token_limit:
                return self._block(
                    LLMBudgetBlockReason.DAILY_TOKEN_LIMIT_EXHAUSTED, call_type
                )

            cost_limit = self._config.llm_daily_cost_limit_usd
            if cost_limit == 0:
                return self._block(
                    LLMBudgetBlockReason.DAILY_COST_LIMIT_EXHAUSTED, call_type
                )
            if self._total_cost_usd >= cost_limit:
                return self._block(
                    LLMBudgetBlockReason.DAILY_COST_LIMIT_EXHAUSTED, call_type
                )

            per_market_limit = self._config.llm_market_hourly_call_limit
            if per_market_limit == 0:
                return self._block(
                    LLMBudgetBlockReason.PER_MARKET_HOURLY_LIMIT_EXHAUSTED, call_type
                )
            if market_key is not None:
                market_calls = self._per_market_hourly.get(market_key, 0)
                if market_calls >= per_market_limit:
                    return self._block(
                        LLMBudgetBlockReason.PER_MARKET_HOURLY_LIMIT_EXHAUSTED,
                        call_type,
                    )

            # WI-52: Atomic counter + token/cost reservation — prevents concurrent bypass
            fallback = self._config.llm_fallback_tokens_per_call
            cost_per_input = Decimal(str(self._config.llm_cost_per_input_token_usd))
            cost_per_output = Decimal(str(self._config.llm_cost_per_output_token_usd))
            reserved_tokens = fallback
            reserved_cost = (cost_per_input * (fallback // 2)) + (
                cost_per_output * (fallback - fallback // 2)
            )

            # Check if reserving this call would exceed token/cost limits
            if (
                token_limit > 0
                and (self._total_tokens_consumed + reserved_tokens) > token_limit
            ):
                return self._block(
                    LLMBudgetBlockReason.DAILY_TOKEN_LIMIT_EXHAUSTED, call_type
                )
            if cost_limit > 0 and (self._total_cost_usd + reserved_cost) > cost_limit:
                return self._block(
                    LLMBudgetBlockReason.DAILY_COST_LIMIT_EXHAUSTED, call_type
                )

            self._global_window = LLMBudgetWindow(
                hourly_calls=self._global_window.hourly_calls + 1,
                daily_calls=self._global_window.daily_calls + 1,
                hourly_window_start=self._hourly_window_start,
                daily_window_start=self._daily_window_start,
            )
            if market_key is not None:
                current = self._per_market_hourly.get(market_key, 0)
                self._per_market_hourly[market_key] = current + 1

            # Reserve token/cost budget atomically
            self._total_tokens_consumed += reserved_tokens
            self._total_cost_usd += reserved_cost
            self._estimated_spend_usd += reserved_cost

            # Emit call metric (fire-and-forget)
            if self._metrics is not None:
                asyncio.ensure_future(self._emit_metrics_for_call(call_type))

            return LLMBudgetDecision(allowed=True, call_type=call_type)

    async def _emit_metrics_for_call(self, call_type: str) -> None:
        """Emit LLM call metric (fire-and-forget, non-blocking)."""
        if self._metrics is not None:
            try:
                await self._metrics.record_llm_call(call_type=call_type)
            except Exception:
                pass

    async def record_usage(
        self,
        *,
        provider: LLMProviderName,
        model_name: str,
        input_tokens: Optional[int] = None,
        output_tokens: Optional[int] = None,
        market_key: Optional[str] = None,
    ) -> LLMUsageRecord:
        """Adjust reserved token/cost budget with actual usage data.

        Call counters and fallback token/cost budget were already reserved
        atomically in check_budget(). This method adjusts the reservation
        to reflect actual usage, adding or removing the delta."""
        async with self._lock:
            self._refresh_windows()

            # Determine actual token counts — use fallback if missing or malformed
            fallback = self._config.llm_fallback_tokens_per_call
            estimated = False

            def _safe_tokens(val: object) -> int | None:
                if val is None:
                    return None
                if not isinstance(val, int):
                    return None
                if val < 0:
                    return None
                return val

            safe_input = _safe_tokens(input_tokens)
            safe_output = _safe_tokens(output_tokens)

            if safe_input is None or safe_output is None:
                # Conservative fallback: assume all tokens = fallback split 50/50
                input_tokens = fallback // 2
                output_tokens = fallback - input_tokens
                estimated = True
            else:
                input_tokens = safe_input
                output_tokens = safe_output

            total_tokens = input_tokens + output_tokens

            # Compute actual estimated cost — ensure Decimal arithmetic
            cost_per_input = Decimal(str(self._config.llm_cost_per_input_token_usd))
            cost_per_output = Decimal(str(self._config.llm_cost_per_output_token_usd))
            actual_cost = (cost_per_input * input_tokens) + (
                cost_per_output * output_tokens
            )

            # Adjust reserved budget: subtract fallback reservation, add actual
            reserved_tokens = fallback
            reserved_cost = (cost_per_input * (fallback // 2)) + (
                cost_per_output * (fallback - fallback // 2)
            )
            token_delta = total_tokens - reserved_tokens
            cost_delta = actual_cost - reserved_cost

            self._total_tokens_consumed += token_delta
            self._total_cost_usd += cost_delta
            self._estimated_spend_usd += cost_delta

            usage = LLMUsageRecord(
                provider=provider,
                model_name=model_name,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                estimated_cost_usd=actual_cost,
                is_estimated=estimated,
            )

            logger.info(
                "llm_usage_recorded",
                provider=provider.value,
                model=model_name,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                estimated_cost_usd=str(actual_cost),
                is_estimated=estimated,
            )

            # Emit metrics (fire-and-forget)
            if self._metrics is not None:
                asyncio.ensure_future(
                    self._emit_usage_metrics(total_tokens, actual_cost)
                )

            return usage

    async def _emit_usage_metrics(
        self, total_tokens: int, estimated_cost: Decimal
    ) -> None:
        """Emit token and spend metrics (non-blocking)."""
        if self._metrics is not None:
            try:
                await self._metrics.record_llm_tokens(total_tokens=total_tokens)
                await self._metrics.record_llm_estimated_spend(cost_usd=estimated_cost)
            except Exception:
                pass

    async def record_provider_error(
        self,
        *,
        market_key: Optional[str] = None,
    ) -> None:
        """Record a provider error (timeout, etc.) without inventing token usage.

        Call counter was already reserved in check_budget().
        This method only logs the error event."""
        logger.warning(
            "llm_provider_error_recorded",
            market_key=_hash_market_key(market_key) if market_key else None,
        )

    async def get_snapshot(self) -> LLMCostGuardSnapshot:
        """Return a secret-free snapshot of current budget state."""
        async with self._lock:
            self._refresh_windows()
            return LLMCostGuardSnapshot(
                budget_enabled=self._config.enable_llm_cost_guard,
                budget_allowed=True,  # snapshot doesn't check — use check_budget
                estimated_spend_usd=self._estimated_spend_usd,
            )

    # -- Internals ---------------------------------------------------------

    def _refresh_windows(self) -> None:
        now = datetime.now(timezone.utc)

        # Refresh hourly window
        if self._hourly_window_start is None or (
            now - self._hourly_window_start
        ) >= timedelta(hours=1):
            self._hourly_window_start = now
            self._global_window = LLMBudgetWindow(
                daily_calls=self._global_window.daily_calls,
                daily_window_start=self._daily_window_start,
                hourly_window_start=now,
            )
            self._per_market_hourly.clear()

        # Refresh daily window
        if self._daily_window_start is None or (
            now - self._daily_window_start
        ) >= timedelta(days=1):
            self._daily_window_start = now
            self._global_window = LLMBudgetWindow(
                hourly_calls=self._global_window.hourly_calls,
                hourly_window_start=self._hourly_window_start,
                daily_window_start=now,
            )
            self._total_tokens_consumed = 0
            self._total_cost_usd = Decimal("0")

    def _block(
        self,
        reason: LLMBudgetBlockReason,
        call_type: str,
    ) -> LLMBudgetDecision:
        logger.warning(
            "llm_budget_blocked",
            reason=reason.value,
            call_type=call_type,
        )
        if self._metrics is not None:
            asyncio.ensure_future(
                self._metrics.record_llm_budget_block(reason=reason.value)
            )
        return LLMBudgetDecision(
            allowed=False, block_reason=reason, call_type=call_type
        )


# ---------------------------------------------------------------------------
# MarketCognitiveCircuitBreaker
# ---------------------------------------------------------------------------


class MarketCognitiveCircuitBreaker:
    """In-memory per-market cognitive state tracker.

    Tracks repeated HOLD, SKIP, invalid JSON, and provider-error outcomes.
    After a configured threshold, places the market in a bounded cooldown.
    """

    def __init__(
        self, config: AppConfig, metrics: Optional[_MetricsRegistry] = None
    ) -> None:
        self._config = config
        self._metrics = metrics
        self._lock = asyncio.Lock()
        self._states: Dict[str, MarketCognitiveState] = {}

    # -- Public API -------------------------------------------------------

    async def check_cooldown(self, market_key: str) -> MarketCooldownDecision:
        """Return whether a market is currently in cooldown."""
        async with self._lock:
            state = self._states.get(market_key)
            if state is None:
                return MarketCooldownDecision(in_cooldown=False)

            if state.cooldown_active_until_utc is None:
                return MarketCooldownDecision(in_cooldown=False)

            now = datetime.now(timezone.utc)
            if now >= state.cooldown_active_until_utc:
                # Cooldown expired — reset state
                state.consecutive_non_actionable = 0
                state.consecutive_invalid = 0
                state.cooldown_active_until_utc = None
                state.cooldown_reason = None
                return MarketCooldownDecision(in_cooldown=False)

            decision = MarketCooldownDecision(
                in_cooldown=True,
                cooldown_reason=state.cooldown_reason,
                expires_at_utc=state.cooldown_active_until_utc,
            )

            # Emit active cooldown count gauge on every check
            if self._metrics is not None:
                count = sum(
                    1
                    for s in self._states.values()
                    if s.cooldown_active_until_utc is not None
                    and now < s.cooldown_active_until_utc
                )
                asyncio.ensure_future(self._metrics.set_active_cooldown_count(count))

            return decision

    async def record_outcome(
        self,
        *,
        market_key: str,
        outcome_type: str,
    ) -> None:
        """Record an evaluation outcome for a market.

        outcome_type must be one of:
        - "hold" — HOLD decision
        - "skip" — SKIP decision
        - "invalid_json" — malformed JSON / validation failure
        - "provider_error" — provider timeout / error
        - "low_confidence" — below confidence threshold
        - "buy" / "sell" — actionable outcomes (reset counters)
        """
        async with self._lock:
            state = self._states.setdefault(
                market_key,
                MarketCognitiveState(market_key=market_key),
            )

            # Check if cooldown is active — if so, skip tracking
            if (
                state.cooldown_active_until_utc is not None
                and datetime.now(timezone.utc) < state.cooldown_active_until_utc
            ):
                return

            actionable = outcome_type in ("buy", "sell")

            if actionable:
                # Reset all counters on actionable outcome
                state.consecutive_non_actionable = 0
                state.consecutive_invalid = 0
                state.last_outcome_type = outcome_type
                return

            non_actionable = outcome_type in ("hold", "skip", "low_confidence")
            is_invalid = outcome_type in ("invalid_json", "provider_error")

            if is_invalid:
                state.consecutive_invalid += 1
                state.consecutive_non_actionable += 1
            elif non_actionable:
                state.consecutive_non_actionable += 1
                state.consecutive_invalid = 0
            else:
                # Unknown outcome type — reset
                state.consecutive_non_actionable = 0
                state.consecutive_invalid = 0

            state.last_outcome_type = outcome_type

            # Check thresholds
            hold_threshold = self._config.llm_repeated_hold_threshold
            invalid_threshold = self._config.llm_repeated_invalid_threshold
            cooldown_seconds = self._config.llm_market_cooldown_seconds  # Decimal

            cooldown_reason: Optional[MarketCooldownReason] = None

            if (
                outcome_type == "hold"
                and state.consecutive_non_actionable >= hold_threshold
            ):
                cooldown_reason = MarketCooldownReason.REPEATED_HOLD
            elif (
                outcome_type == "skip"
                and state.consecutive_non_actionable >= hold_threshold
            ):
                cooldown_reason = MarketCooldownReason.REPEATED_SKIP
            elif (
                outcome_type == "low_confidence"
                and state.consecutive_non_actionable >= hold_threshold
            ):
                cooldown_reason = MarketCooldownReason.REPEATED_LOW_CONFIDENCE
            elif (
                outcome_type == "invalid_json"
                and state.consecutive_invalid >= invalid_threshold
            ):
                cooldown_reason = MarketCooldownReason.REPEATED_INVALID_JSON
            elif (
                outcome_type == "provider_error"
                and state.consecutive_invalid >= invalid_threshold
            ):
                cooldown_reason = MarketCooldownReason.REPEATED_PROVIDER_ERROR

            if cooldown_reason is not None:
                now = datetime.now(timezone.utc)
                state.cooldown_active_until_utc = now + timedelta(
                    seconds=int(cooldown_seconds)  # Decimal → int for timedelta
                )
                state.cooldown_reason = cooldown_reason

                logger.warning(
                    "market_cooldown_activated",
                    market_key=_hash_market_key(market_key),
                    reason=cooldown_reason.value,
                    expires_in_seconds=str(cooldown_seconds),
                )

                # Emit cooldown block metric
                if self._metrics is not None:
                    asyncio.ensure_future(self._metrics.record_llm_cooldown_block())

    async def reset_market(self, market_key: str) -> None:
        """Reset cognitive state for a market (e.g. after cooldown expiry or material change)."""
        async with self._lock:
            self._states.pop(market_key, None)

    async def get_active_cooldown_count(self) -> int:
        """Return number of markets currently in cooldown."""
        async with self._lock:
            now = datetime.now(timezone.utc)
            count = 0
            expired_keys = []
            for key, state in self._states.items():
                if state.cooldown_active_until_utc is not None:
                    if now < state.cooldown_active_until_utc:
                        count += 1
                    else:
                        expired_keys.append(key)
            # Clean up expired
            for key in expired_keys:
                state = self._states[key]
                state.cooldown_active_until_utc = None
                state.cooldown_reason = None
                state.consecutive_non_actionable = 0
                state.consecutive_invalid = 0

            # Emit active cooldown count metric
            if self._metrics is not None:
                asyncio.ensure_future(self._metrics.set_active_cooldown_count(count))

            return count
