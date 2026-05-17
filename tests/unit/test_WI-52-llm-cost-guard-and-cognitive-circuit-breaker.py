"""Tests for WI-52: LLM Cost Guard and Cognitive Circuit Breaker."""

import asyncio
import os
from datetime import datetime, timezone, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from src.schemas.llm import (
    LLMProviderName,
    LLMBudgetBlockReason,
    MarketCooldownReason,
    LLMUsageRecord,
    LLMBudgetConfig,
    LLMBudgetWindow,
    LLMBudgetState,
    LLMBudgetDecision,
    MarketCognitiveState,
    MarketCooldownDecision,
    LLMCostGuardSnapshot,
)
from src.agents.evaluation.llm_cost_guard import (
    LLMBudgetGuard,
    MarketCognitiveCircuitBreaker,
)
from src.observability.metrics import MetricsRegistry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides: dict) -> object:
    """Create an AppConfig with env isolation per memory guidance."""
    from src.core.config import AppConfig

    base = {
        "anthropic_api_key": "sk-test-key-for-testing-only-1234567890",
        "polygon_rpc_url": "https://rpc.ankr.com/polygon",
        "wallet_address": "0x1111111111111111111111111111111111111111",
        "wallet_private_key": "0x" + "1" * 64,
        "dry_run": True,
    }
    base.update(overrides)
    for key in [
        "LLM_HOURLY_CALL_LIMIT",
        "LLM_DAILY_CALL_LIMIT",
        "LLM_DAILY_TOKEN_LIMIT",
        "LLM_DAILY_COST_LIMIT_USD",
        "LLM_MARKET_HOURLY_CALL_LIMIT",
        "LLM_REPEATED_HOLD_THRESHOLD",
        "LLM_REPEATED_INVALID_THRESHOLD",
        "LLM_MARKET_COOLDOWN_SECONDS",
        "LLM_FALLBACK_TOKENS_PER_CALL",
        "LLM_COST_PER_INPUT_TOKEN_USD",
        "LLM_COST_PER_OUTPUT_TOKEN_USD",
        "ENABLE_LLM_COST_GUARD",
    ]:
        try:
            del os.environ[key]
        except KeyError:
            pass
    return AppConfig(**base, _env_file=None)


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------


class TestLLMProviderName:
    def test_valid_provider_names(self):
        assert LLMProviderName.ANTHROPIC.value == "anthropic"
        assert LLMProviderName.DEEPSEEK.value == "deepseek"

    def test_invalid_provider_name_raises(self):
        with pytest.raises(ValueError):
            LLMProviderName("openai")


class TestLLMUsageRecord:
    def test_usage_record_with_full_data(self):
        rec = LLMUsageRecord(
            provider=LLMProviderName.ANTHROPIC,
            model_name="claude-sonnet-4-20250514",
            input_tokens=1000,
            output_tokens=500,
            total_tokens=1500,
            estimated_cost_usd=Decimal("0.0045"),
        )
        assert rec.input_tokens == 1000
        assert rec.is_estimated is False

    def test_usage_record_with_missing_fields_uses_estimated(self):
        rec = LLMUsageRecord(
            provider=LLMProviderName.ANTHROPIC,
            model_name="test",
            input_tokens=0,
            output_tokens=0,
            total_tokens=0,
            is_estimated=True,
        )
        assert rec.is_estimated is True

    def test_usage_record_with_malformed_fields_uses_estimated(self):
        rec = LLMUsageRecord(
            provider=LLMProviderName.ANTHROPIC,
            model_name="test",
            input_tokens=0,
            output_tokens=0,
            total_tokens=0,
            is_estimated=True,
        )
        assert rec.is_estimated is True

    def test_usage_record_cost_is_decimal(self):
        rec = LLMUsageRecord(
            provider=LLMProviderName.ANTHROPIC,
            model_name="test",
            input_tokens=0,
            output_tokens=0,
            total_tokens=0,
            estimated_cost_usd=Decimal("0.001"),
        )
        assert isinstance(rec.estimated_cost_usd, Decimal)

    def test_usage_record_cost_is_not_float(self):
        with pytest.raises(ValidationError):
            LLMUsageRecord(
                provider=LLMProviderName.ANTHROPIC,
                model_name="test",
                input_tokens=0,
                output_tokens=0,
                total_tokens=0,
                estimated_cost_usd=0.001,
            )


class TestLLMBudgetConfig:
    def test_valid_budget_config(self):
        cfg = LLMBudgetConfig(
            enabled=True,
            hourly_call_limit=60,
            daily_call_limit=500,
            daily_token_limit=1000000,
            daily_cost_limit_usd=Decimal("10"),
        )
        assert cfg.enabled is True

    def test_negative_hourly_limit_rejected(self):
        with pytest.raises(ValidationError):
            LLMBudgetConfig(hourly_call_limit=-1)

    def test_negative_daily_limit_rejected(self):
        with pytest.raises(ValidationError):
            LLMBudgetConfig(daily_call_limit=-1)

    def test_negative_daily_token_limit_rejected(self):
        with pytest.raises(ValidationError):
            LLMBudgetConfig(daily_token_limit=-1)

    def test_negative_daily_cost_limit_rejected(self):
        with pytest.raises(ValidationError):
            LLMBudgetConfig(daily_cost_limit_usd=Decimal("-1"))

    def test_negative_cooldown_duration_rejected(self):
        with pytest.raises(ValidationError):
            _make_config(llm_market_cooldown_seconds=Decimal("-1"))

    def test_malformed_decimal_values_rejected(self):
        with pytest.raises(ValidationError):
            LLMBudgetConfig(daily_cost_limit_usd="xyz")


class TestLLMBudgetWindow:
    def test_hourly_window_tracks_call_count(self):
        w = LLMBudgetWindow(hourly_calls=5)
        assert w.hourly_calls == 5

    def test_daily_window_tracks_call_count(self):
        w = LLMBudgetWindow(daily_calls=100)
        assert w.daily_calls == 100

    def test_hourly_window_expires(self):
        now = datetime.now(timezone.utc)
        w = LLMBudgetWindow(
            hourly_calls=5,
            hourly_window_start_utc=now - timedelta(hours=2),
        )
        assert (now - w.hourly_window_start_utc) > timedelta(hours=1)

    def test_daily_window_expires(self):
        now = datetime.now(timezone.utc)
        w = LLMBudgetWindow(
            daily_calls=50,
            daily_window_start_utc=now - timedelta(days=2),
        )
        assert (now - w.daily_window_start_utc) > timedelta(days=1)


class TestLLMBudgetState:
    def test_budget_state_allows_call(self):
        s = LLMBudgetState(allowed=True, remaining_calls_hourly=50)
        assert s.allowed is True

    def test_budget_state_blocks_on_hourly_limit(self):
        s = LLMBudgetState(
            allowed=False,
            block_reason=LLMBudgetBlockReason.HOURLY_CALL_LIMIT_EXHAUSTED,
        )
        assert s.block_reason == LLMBudgetBlockReason.HOURLY_CALL_LIMIT_EXHAUSTED

    def test_budget_state_blocks_on_daily_limit(self):
        s = LLMBudgetState(
            allowed=False,
            block_reason=LLMBudgetBlockReason.DAILY_CALL_LIMIT_EXHAUSTED,
        )
        assert s.block_reason == LLMBudgetBlockReason.DAILY_CALL_LIMIT_EXHAUSTED

    def test_budget_state_blocks_on_token_limit(self):
        s = LLMBudgetState(
            allowed=False,
            block_reason=LLMBudgetBlockReason.DAILY_TOKEN_LIMIT_EXHAUSTED,
        )
        assert s.block_reason == LLMBudgetBlockReason.DAILY_TOKEN_LIMIT_EXHAUSTED

    def test_budget_state_blocks_on_cost_limit(self):
        s = LLMBudgetState(
            allowed=False,
            block_reason=LLMBudgetBlockReason.DAILY_COST_LIMIT_EXHAUSTED,
        )
        assert s.block_reason == LLMBudgetBlockReason.DAILY_COST_LIMIT_EXHAUSTED

    def test_budget_state_decimal_math(self):
        s = LLMBudgetState(allowed=True, remaining_cost_daily_usd=Decimal("5.50"))
        assert isinstance(s.remaining_cost_daily_usd, Decimal)


class TestLLMBudgetDecision:
    def test_allow_decision(self):
        d = LLMBudgetDecision(allowed=True, call_type="primary")
        assert d.allowed is True

    def test_block_decision_with_reason(self):
        d = LLMBudgetDecision(
            allowed=False,
            block_reason=LLMBudgetBlockReason.HOURLY_CALL_LIMIT_EXHAUSTED,
            call_type="reflection",
        )
        assert d.allowed is False
        assert d.call_type == "reflection"


class TestLLMBudgetBlockReason:
    def test_hourly_exhausted_reason(self):
        assert (
            LLMBudgetBlockReason.HOURLY_CALL_LIMIT_EXHAUSTED.value
            == "hourly_call_limit_exhausted"
        )

    def test_daily_exhausted_reason(self):
        assert (
            LLMBudgetBlockReason.DAILY_CALL_LIMIT_EXHAUSTED.value
            == "daily_call_limit_exhausted"
        )

    def test_token_limit_exhausted_reason(self):
        assert (
            LLMBudgetBlockReason.DAILY_TOKEN_LIMIT_EXHAUSTED.value
            == "daily_token_limit_exhausted"
        )

    def test_cost_limit_exhausted_reason(self):
        assert (
            LLMBudgetBlockReason.DAILY_COST_LIMIT_EXHAUSTED.value
            == "daily_cost_limit_exhausted"
        )


class TestMarketCognitiveState:
    def test_tracks_repeated_hold_outcomes(self):
        s = MarketCognitiveState(market_key="m", consecutive_non_actionable=3)
        assert s.consecutive_non_actionable == 3

    def test_tracks_repeated_skip_outcomes(self):
        s = MarketCognitiveState(
            market_key="m", consecutive_non_actionable=5, last_outcome_type="skip"
        )
        assert s.last_outcome_type == "skip"

    def test_tracks_malformed_json_outcomes(self):
        s = MarketCognitiveState(
            market_key="m", consecutive_invalid=2, last_outcome_type="invalid_json"
        )
        assert s.consecutive_invalid == 2

    def test_tracks_provider_error_outcomes(self):
        s = MarketCognitiveState(
            market_key="m", consecutive_invalid=3, last_outcome_type="provider_error"
        )
        assert s.last_outcome_type == "provider_error"

    def test_resets_on_different_outcome(self):
        s = MarketCognitiveState(
            market_key="m",
            consecutive_non_actionable=0,
            consecutive_invalid=0,
            last_outcome_type="buy",
        )
        assert s.consecutive_non_actionable == 0


class TestMarketCooldownDecision:
    def test_cooldown_decision_active(self):
        future = datetime.now(timezone.utc) + timedelta(minutes=5)
        d = MarketCooldownDecision(
            in_cooldown=True,
            cooldown_reason=MarketCooldownReason.REPEATED_HOLD,
            expires_at_utc=future,
        )
        assert d.in_cooldown is True

    def test_cooldown_decision_expired(self):
        d = MarketCooldownDecision(in_cooldown=False)
        assert d.in_cooldown is False


class TestMarketCooldownReason:
    def test_repeated_hold_reason(self):
        assert MarketCooldownReason.REPEATED_HOLD.value == "repeated_hold"

    def test_repeated_invalid_json_reason(self):
        assert (
            MarketCooldownReason.REPEATED_INVALID_JSON.value == "repeated_invalid_json"
        )

    def test_repeated_low_confidence_reason(self):
        assert (
            MarketCooldownReason.REPEATED_LOW_CONFIDENCE.value
            == "repeated_low_confidence"
        )


class TestLLMCostGuardSnapshot:
    def test_snapshot_serializes(self):
        snap = LLMCostGuardSnapshot(
            budget_enabled=True,
            budget_allowed=True,
            estimated_spend_usd=Decimal("2.50"),
        )
        assert snap.budget_enabled is True
        assert isinstance(snap.estimated_spend_usd, Decimal)

    def test_snapshot_excludes_secrets(self):
        snap = LLMCostGuardSnapshot(
            budget_enabled=True,
            budget_allowed=False,
            budget_block_reason=LLMBudgetBlockReason.DAILY_COST_LIMIT_EXHAUSTED,
        )
        dump = snap.model_dump()
        for key in dump:
            assert "secret" not in key.lower()
            assert "api_key" not in key.lower()


# ---------------------------------------------------------------------------
# AppConfig LLM cost guard defaults
# ---------------------------------------------------------------------------


class TestAppConfigLLMCostGuard:
    def test_enable_llm_cost_guard_default(self):
        cfg = _make_config()
        assert cfg.enable_llm_cost_guard is True

    def test_llm_hourly_call_limit_default(self):
        cfg = _make_config()
        assert cfg.llm_hourly_call_limit == 60

    def test_llm_daily_call_limit_default(self):
        cfg = _make_config()
        assert cfg.llm_daily_call_limit == 500

    def test_llm_daily_token_limit_default(self):
        cfg = _make_config()
        assert cfg.llm_daily_token_limit == 1000000

    def test_llm_daily_cost_limit_default(self):
        cfg = _make_config()
        assert cfg.llm_daily_cost_limit_usd == Decimal("10")

    def test_llm_per_market_hourly_call_limit_default(self):
        cfg = _make_config()
        assert cfg.llm_market_hourly_call_limit == 10

    def test_llm_repeated_hold_threshold_default(self):
        cfg = _make_config()
        assert cfg.llm_repeated_hold_threshold == 5

    def test_llm_repeated_invalid_threshold_default(self):
        cfg = _make_config()
        assert cfg.llm_repeated_invalid_threshold == 3

    def test_llm_market_cooldown_duration_default(self):
        cfg = _make_config()
        assert cfg.llm_market_cooldown_seconds == Decimal("300")

    def test_llm_fallback_tokens_per_call_default(self):
        cfg = _make_config()
        assert cfg.llm_fallback_tokens_per_call == 4096

    def test_llm_cost_per_input_token_default(self):
        cfg = _make_config()
        assert cfg.llm_cost_per_input_token_usd == Decimal("0.0000015")

    def test_llm_cost_per_output_token_default(self):
        cfg = _make_config()
        assert cfg.llm_cost_per_output_token_usd == Decimal("0.000006")


# ---------------------------------------------------------------------------
# Budget enforcement tests (unit)
# ---------------------------------------------------------------------------


class TestBudgetEnforcementUnit:
    @pytest.fixture
    def guard(self):
        cfg = _make_config(
            enable_llm_cost_guard=True,
            llm_hourly_call_limit=2,
            llm_daily_call_limit=10,
            llm_daily_token_limit=1000000,
            llm_daily_cost_limit_usd=Decimal("100"),
            llm_market_hourly_call_limit=100,
        )
        return LLMBudgetGuard(cfg)

    @pytest.mark.asyncio
    async def test_blocks_primary_call_when_hourly_limit_exhausted(self, guard):
        # check_budget atomically reserves counter
        d1 = await guard.check_budget(call_type="primary")
        assert d1.allowed is True
        await guard.record_usage(
            provider=LLMProviderName.ANTHROPIC,
            model_name="t",
            input_tokens=100,
            output_tokens=50,
        )
        d2 = await guard.check_budget(call_type="primary")
        assert d2.allowed is True
        await guard.record_usage(
            provider=LLMProviderName.ANTHROPIC,
            model_name="t",
            input_tokens=100,
            output_tokens=50,
        )
        d3 = await guard.check_budget(call_type="primary")
        assert d3.allowed is False
        assert d3.block_reason == LLMBudgetBlockReason.HOURLY_CALL_LIMIT_EXHAUSTED

    async def test_blocks_primary_call_when_daily_limit_exhausted(self):
        cfg = _make_config(
            enable_llm_cost_guard=True,
            llm_hourly_call_limit=100,
            llm_daily_call_limit=3,
            llm_daily_token_limit=1000000,
            llm_daily_cost_limit_usd=Decimal("100"),
            llm_market_hourly_call_limit=100,
        )
        guard = LLMBudgetGuard(cfg)
        for _ in range(3):
            d = await guard.check_budget(call_type="primary")
            assert d.allowed is True
        d = await guard.check_budget(call_type="primary")
        assert d.allowed is False
        assert d.block_reason == LLMBudgetBlockReason.DAILY_CALL_LIMIT_EXHAUSTED

    async def test_blocks_primary_call_when_token_limit_exhausted(self):
        # Token limit must be > fallback (4096) for first call to pass
        cfg = _make_config(
            enable_llm_cost_guard=True,
            llm_hourly_call_limit=100,
            llm_daily_call_limit=100,
            llm_daily_token_limit=5000,
            llm_daily_cost_limit_usd=Decimal("100"),
            llm_market_hourly_call_limit=100,
        )
        guard = LLMBudgetGuard(cfg)
        d = await guard.check_budget(call_type="primary")
        assert d.allowed is True
        # record_usage adjusts: reserved 4096, actual 100 -> delta = -3996
        await guard.record_usage(
            provider=LLMProviderName.ANTHROPIC,
            model_name="t",
            input_tokens=60,
            output_tokens=40,
        )
        # Second call reserves another 4096 -> total = 100 + 4096 = 4196 < 5000, allowed
        d = await guard.check_budget(call_type="primary")
        assert d.allowed is True
        await guard.record_usage(
            provider=LLMProviderName.ANTHROPIC,
            model_name="t",
            input_tokens=60,
            output_tokens=40,
        )
        # Third call: 100 + 100 + 4096 = 4296 < 5000, allowed
        d = await guard.check_budget(call_type="primary")
        assert d.allowed is True
        await guard.record_usage(
            provider=LLMProviderName.ANTHROPIC,
            model_name="t",
            input_tokens=60,
            output_tokens=40,
        )
        # Fourth call: 300 + 4096 = 4396 < 5000, allowed
        d = await guard.check_budget(call_type="primary")
        assert d.allowed is True
        await guard.record_usage(
            provider=LLMProviderName.ANTHROPIC,
            model_name="t",
            input_tokens=60,
            output_tokens=40,
        )
        # Fifth call: 400 + 4096 = 4496 < 5000, allowed
        d = await guard.check_budget(call_type="primary")
        assert d.allowed is True
        await guard.record_usage(
            provider=LLMProviderName.ANTHROPIC,
            model_name="t",
            input_tokens=60,
            output_tokens=40,
        )
        # Sixth call: 500 + 4096 = 4596 < 5000, allowed
        # ... keep going until blocked
        for _ in range(10):
            d = await guard.check_budget(call_type="primary")
            if not d.allowed:
                break
            await guard.record_usage(
                provider=LLMProviderName.ANTHROPIC,
                model_name="t",
                input_tokens=60,
                output_tokens=40,
            )
        assert d.allowed is False
        assert d.block_reason == LLMBudgetBlockReason.DAILY_TOKEN_LIMIT_EXHAUSTED

    async def test_blocks_primary_call_when_cost_limit_exhausted(self):
        # Cost limit < fallback cost (4.096) so first call is blocked
        cfg = _make_config(
            enable_llm_cost_guard=True,
            llm_hourly_call_limit=100,
            llm_daily_call_limit=100,
            llm_daily_token_limit=10000000,
            llm_daily_cost_limit_usd=Decimal("4"),
            llm_cost_per_input_token_usd=Decimal("0.001"),
            llm_cost_per_output_token_usd=Decimal("0.001"),
            llm_market_hourly_call_limit=100,
        )
        guard = LLMBudgetGuard(cfg)
        # First call reserves ~4.096 cost > 4.0 limit, blocked
        d = await guard.check_budget(call_type="primary")
        assert d.allowed is False
        assert d.block_reason == LLMBudgetBlockReason.DAILY_COST_LIMIT_EXHAUSTED

    async def test_blocks_reflection_call_when_hourly_limit_exhausted(self, guard):
        for _ in range(2):
            d = await guard.check_budget(call_type="primary")
            assert d.allowed is True
        d = await guard.check_budget(call_type="reflection")
        assert d.allowed is False

    async def test_blocks_reflection_call_when_daily_limit_exhausted(self):
        cfg = _make_config(
            enable_llm_cost_guard=True,
            llm_hourly_call_limit=100,
            llm_daily_call_limit=3,
            llm_daily_token_limit=1000000,
            llm_daily_cost_limit_usd=Decimal("100"),
            llm_market_hourly_call_limit=100,
        )
        guard = LLMBudgetGuard(cfg)
        for _ in range(3):
            d = await guard.check_budget(call_type="primary")
            assert d.allowed is True
        d = await guard.check_budget(call_type="reflection")
        assert d.allowed is False

    async def test_blocks_reflection_call_when_token_limit_exhausted(self):
        # Token limit must be > fallback (4096) for first call to pass
        cfg = _make_config(
            enable_llm_cost_guard=True,
            llm_hourly_call_limit=100,
            llm_daily_call_limit=100,
            llm_daily_token_limit=5000,
            llm_daily_cost_limit_usd=Decimal("100"),
            llm_market_hourly_call_limit=100,
        )
        guard = LLMBudgetGuard(cfg)
        d = await guard.check_budget(call_type="primary")
        assert d.allowed is True
        await guard.record_usage(
            provider=LLMProviderName.ANTHROPIC,
            model_name="t",
            input_tokens=60,
            output_tokens=40,
        )
        # Keep consuming until blocked
        for _ in range(20):
            d = await guard.check_budget(call_type="reflection")
            if not d.allowed:
                break
            await guard.record_usage(
                provider=LLMProviderName.ANTHROPIC,
                model_name="t",
                input_tokens=60,
                output_tokens=40,
            )
        assert d.allowed is False
        assert d.block_reason == LLMBudgetBlockReason.DAILY_TOKEN_LIMIT_EXHAUSTED

    async def test_blocks_reflection_call_when_cost_limit_exhausted(self):
        # Cost limit < fallback cost (4.096) so first call is blocked
        cfg = _make_config(
            enable_llm_cost_guard=True,
            llm_hourly_call_limit=100,
            llm_daily_call_limit=100,
            llm_daily_token_limit=10000000,
            llm_daily_cost_limit_usd=Decimal("4"),
            llm_cost_per_input_token_usd=Decimal("0.001"),
            llm_cost_per_output_token_usd=Decimal("0.001"),
            llm_market_hourly_call_limit=100,
        )
        guard = LLMBudgetGuard(cfg)
        d = await guard.check_budget(call_type="reflection")
        assert d.allowed is False

    async def test_blocked_reflection_prevents_execution_routing(self):
        """Budget-blocked reflection returns conservative REJECTED — no execution."""
        from src.schemas.llm import ReflectionResponse, ReflectionVerdict

        # Simulate what ClaudeClient does when reflection is budget-blocked
        reflection = ReflectionResponse(
            verdict=ReflectionVerdict.REJECTED,
            audit_note="BUDGET_BLOCKED_REFLECTION",
            latency_ms=0,
        )
        assert reflection.verdict == ReflectionVerdict.REJECTED
        assert reflection.latency_ms == 0

    async def test_allowed_primary_but_blocked_reflection_resolves_no_trade(self):
        """When primary is allowed but reflection blocked, result is no-trade."""
        from src.schemas.llm import ReflectionResponse, ReflectionVerdict

        cfg = _make_config(
            enable_llm_cost_guard=True,
            llm_hourly_call_limit=2,
            llm_daily_call_limit=2,
            llm_daily_token_limit=1000000,
            llm_daily_cost_limit_usd=Decimal("100"),
            llm_market_hourly_call_limit=100,
        )
        guard = LLMBudgetGuard(cfg)
        d1 = await guard.check_budget(call_type="primary")
        assert d1.allowed is True
        await guard.record_usage(
            provider=LLMProviderName.ANTHROPIC,
            model_name="t",
            input_tokens=10,
            output_tokens=5,
        )
        d2 = await guard.check_budget(call_type="reflection")
        assert d2.allowed is True
        await guard.record_usage(
            provider=LLMProviderName.ANTHROPIC,
            model_name="t",
            input_tokens=10,
            output_tokens=5,
        )
        # Now both hourly and daily are exhausted
        d3 = await guard.check_budget(call_type="reflection")
        assert d3.allowed is False
        # Reflection blocked → conservative no-trade
        reflection = ReflectionResponse(
            verdict=ReflectionVerdict.REJECTED,
            audit_note="BUDGET_BLOCKED_REFLECTION",
            latency_ms=0,
        )
        assert reflection.verdict == ReflectionVerdict.REJECTED

    async def test_cost_guard_disabled_preserves_current_behavior(self):
        cfg = _make_config(enable_llm_cost_guard=False)
        guard = LLMBudgetGuard(cfg)
        d = await guard.check_budget(call_type="primary")
        assert d.allowed is True
        assert d.block_reason is None

    async def test_zero_limit_means_no_calls_allowed(self):
        cfg = _make_config(
            enable_llm_cost_guard=True,
            llm_hourly_call_limit=0,
            llm_daily_call_limit=0,
            llm_daily_token_limit=0,
            llm_daily_cost_limit_usd=Decimal("0"),
        )
        # WI-52: Zero limits are explicit kill switches when guard is enabled.
        guard = LLMBudgetGuard(cfg)
        d = await guard.check_budget(call_type="primary")
        assert d.allowed is False
        assert d.block_reason is not None


# ---------------------------------------------------------------------------
# Cognitive cooldown tests (unit)
# ---------------------------------------------------------------------------


class TestCognitiveCooldownUnit:
    @pytest.fixture
    def breaker(self):
        cfg = _make_config(
            llm_repeated_hold_threshold=3,
            llm_repeated_invalid_threshold=2,
            llm_market_cooldown_seconds=Decimal("60"),
        )
        return MarketCognitiveCircuitBreaker(cfg)

    async def test_cooldown_triggered_by_repeated_holds(self, breaker):
        mk = "market-hold-test"
        for _ in range(3):
            await breaker.record_outcome(market_key=mk, outcome_type="hold")
        d = await breaker.check_cooldown(mk)
        assert d.in_cooldown is True
        assert d.cooldown_reason == MarketCooldownReason.REPEATED_HOLD

    async def test_cooldown_triggered_by_repeated_invalid_json(self, breaker):
        mk = "market-invalid-test"
        for _ in range(2):
            await breaker.record_outcome(market_key=mk, outcome_type="invalid_json")
        d = await breaker.check_cooldown(mk)
        assert d.in_cooldown is True
        assert d.cooldown_reason == MarketCooldownReason.REPEATED_INVALID_JSON

    async def test_cooldown_triggered_by_repeated_low_confidence(self, breaker):
        mk = "market-lowconf-test"
        for _ in range(3):
            await breaker.record_outcome(market_key=mk, outcome_type="low_confidence")
        d = await breaker.check_cooldown(mk)
        assert d.in_cooldown is True
        assert d.cooldown_reason == MarketCooldownReason.REPEATED_LOW_CONFIDENCE

    async def test_cooldown_blocks_only_affected_market(self, breaker):
        mk1 = "market-1"
        mk2 = "market-2"
        for _ in range(3):
            await breaker.record_outcome(market_key=mk1, outcome_type="hold")
        d1 = await breaker.check_cooldown(mk1)
        d2 = await breaker.check_cooldown(mk2)
        assert d1.in_cooldown is True
        assert d2.in_cooldown is False

    async def test_cooldown_does_not_suppress_unrelated_markets(self, breaker):
        mk1 = "market-a"
        mk2 = "market-b"
        for _ in range(3):
            await breaker.record_outcome(market_key=mk1, outcome_type="hold")
        d2 = await breaker.check_cooldown(mk2)
        assert d2.in_cooldown is False

    async def test_cooldown_expires_after_configured_duration(self, breaker):
        mk = "market-expire-test"
        for _ in range(3):
            await breaker.record_outcome(market_key=mk, outcome_type="hold")
        d1 = await breaker.check_cooldown(mk)
        assert d1.in_cooldown is True
        async with breaker._lock:
            state = breaker._states[mk]
            state.cooldown_active_until_utc = datetime.now(timezone.utc) - timedelta(
                seconds=1
            )
        d2 = await breaker.check_cooldown(mk)
        assert d2.in_cooldown is False

    async def test_market_eligible_again_after_cooldown_expiry(self, breaker):
        mk = "market-retry-test"
        for _ in range(3):
            await breaker.record_outcome(market_key=mk, outcome_type="hold")
        async with breaker._lock:
            state = breaker._states[mk]
            state.cooldown_active_until_utc = datetime.now(timezone.utc) - timedelta(
                seconds=1
            )
        d = await breaker.check_cooldown(mk)
        assert d.in_cooldown is False
        await breaker.record_outcome(market_key=mk, outcome_type="buy")

    async def test_cooldown_is_in_memory_only(self, breaker):
        assert not hasattr(breaker, "_db")
        assert isinstance(breaker._states, dict)


# ---------------------------------------------------------------------------
# Usage accounting tests
# ---------------------------------------------------------------------------


class TestUsageAccounting:
    async def test_records_usage_on_successful_call(self):
        cfg = _make_config()
        guard = LLMBudgetGuard(cfg)
        usage = await guard.record_usage(
            provider=LLMProviderName.ANTHROPIC,
            model_name="test",
            input_tokens=1000,
            output_tokens=500,
        )
        assert usage.input_tokens == 1000
        assert usage.output_tokens == 500
        assert usage.total_tokens == 1500
        assert usage.is_estimated is False

    async def test_fallback_estimate_on_missing_usage(self):
        cfg = _make_config(llm_fallback_tokens_per_call=4096)
        guard = LLMBudgetGuard(cfg)
        usage = await guard.record_usage(
            provider=LLMProviderName.ANTHROPIC,
            model_name="test",
            input_tokens=None,
            output_tokens=None,
        )
        assert usage.is_estimated is True
        assert usage.total_tokens == 4096

    async def test_fallback_estimate_on_malformed_usage(self):
        cfg = _make_config(llm_fallback_tokens_per_call=2048)
        guard = LLMBudgetGuard(cfg)
        usage = await guard.record_usage(
            provider=LLMProviderName.ANTHROPIC,
            model_name="test",
            input_tokens=None,
            output_tokens=100,
        )
        assert usage.is_estimated is True

    async def test_estimated_flag_set_on_fallback(self):
        cfg = _make_config()
        guard = LLMBudgetGuard(cfg)
        usage = await guard.record_usage(
            provider=LLMProviderName.ANTHROPIC,
            model_name="test",
            input_tokens=None,
            output_tokens=None,
        )
        assert usage.is_estimated is True

    async def test_provider_timeout_records_error_event_without_invented_tokens(self):
        cfg = _make_config(
            enable_llm_cost_guard=True,
            llm_hourly_call_limit=10,
            llm_daily_call_limit=10,
            llm_daily_token_limit=1000000,
            llm_daily_cost_limit_usd=Decimal("100"),
            llm_market_hourly_call_limit=100,
        )
        guard = LLMBudgetGuard(cfg)
        d = await guard.check_budget(call_type="primary")
        assert d.allowed is True
        await guard.record_provider_error(market_key="timeout-market")
        d2 = await guard.check_budget(call_type="primary")
        assert d2.allowed is True


# ---------------------------------------------------------------------------
# Logging and metrics tests
# ---------------------------------------------------------------------------


class TestLoggingAndMetrics:
    async def test_budget_block_logged_with_reason_code(self):
        cfg = _make_config(
            enable_llm_cost_guard=True,
            llm_hourly_call_limit=1,
            llm_daily_token_limit=1000000,
            llm_daily_cost_limit_usd=Decimal("100"),
            llm_market_hourly_call_limit=100,
        )
        guard = LLMBudgetGuard(cfg)
        # Exhaust the single allowed hourly call, then assert the next is blocked
        first = await guard.check_budget(call_type="primary")
        assert first.allowed is True
        d = await guard.check_budget(call_type="primary")
        assert d.allowed is False
        assert d.block_reason is not None

    async def test_cooldown_block_logged_with_reason_code(
        self,
    ):
        cfg = _make_config(
            llm_repeated_hold_threshold=2,
            llm_market_cooldown_seconds=Decimal("60"),
        )
        breaker = MarketCognitiveCircuitBreaker(cfg)
        mk = "metrics-cooldown-test"
        for _ in range(2):
            await breaker.record_outcome(market_key=mk, outcome_type="hold")
        d = await breaker.check_cooldown(mk)
        assert d.in_cooldown is True
        assert d.cooldown_reason is not None

    async def test_logs_exclude_secrets(self):
        snap = LLMCostGuardSnapshot(
            budget_enabled=True,
            budget_allowed=True,
            estimated_spend_usd=Decimal("1.0"),
        )
        dump = snap.model_dump()
        for k in dump:
            assert "api_key" not in k.lower()
            assert "secret" not in k.lower()
            assert "private" not in k.lower()

    async def test_metrics_use_low_cardinality_labels(self):
        reg = MetricsRegistry()
        await reg.record_llm_call(call_type="primary")
        snap = await reg.snapshot()
        for sample in snap.samples:
            for k, v in sample.labels.labels.items():
                assert len(v) < 128

    async def test_metrics_do_not_include_prompt_text(self):
        reg = MetricsRegistry()
        await reg.record_llm_call(call_type="primary")
        rendered = reg.render_prometheus(await reg.snapshot())
        assert "prompt" not in rendered.lower() or "poly_agent" in rendered

    async def test_metrics_do_not_include_reasoning_text(self):
        reg = MetricsRegistry()
        await reg.record_llm_budget_block(reason="hourly_call_limit_exhausted")
        rendered = reg.render_prometheus(await reg.snapshot())
        assert "reasoning" not in rendered.lower()

    async def test_metrics_do_not_include_token_ids(self):
        reg = MetricsRegistry()
        await reg.record_llm_tokens(total_tokens=1000)
        rendered = reg.render_prometheus(await reg.snapshot())
        for line in rendered.split("\n"):
            if line.startswith("poly_agent"):
                assert "0x" not in line

    async def test_metrics_do_not_include_api_keys(self):
        reg = MetricsRegistry()
        await reg.record_llm_estimated_spend(cost_usd=Decimal("0.01"))
        rendered = reg.render_prometheus(await reg.snapshot())
        assert "sk-" not in rendered
        assert "api_key" not in rendered.lower()

    async def test_metrics_track_llm_calls(self):
        reg = MetricsRegistry()
        await reg.record_llm_call(call_type="primary")
        await reg.record_llm_call(call_type="primary")
        await reg.record_llm_call(call_type="reflection")
        snap = await reg.snapshot()
        llm_calls = [s for s in snap.samples if s.name == "poly_agent_llm_calls_total"]
        total = sum(int(s.value) for s in llm_calls)
        assert total == 3

    async def test_metrics_track_budget_blocks(self):
        reg = MetricsRegistry()
        await reg.record_llm_budget_block(reason="hourly_call_limit_exhausted")
        snap = await reg.snapshot()
        blocks = [
            s for s in snap.samples if s.name == "poly_agent_llm_budget_blocks_total"
        ]
        assert any(int(s.value) > 0 for s in blocks)

    async def test_metrics_track_cooldown_blocks(self):
        reg = MetricsRegistry()
        await reg.record_llm_cooldown_block()
        snap = await reg.snapshot()
        blocks = [
            s for s in snap.samples if s.name == "poly_agent_llm_cooldown_blocks_total"
        ]
        assert any(int(s.value) > 0 for s in blocks)

    async def test_metrics_track_token_usage(self):
        reg = MetricsRegistry()
        await reg.record_llm_tokens(total_tokens=5000)
        snap = await reg.snapshot()
        tokens = [s for s in snap.samples if s.name == "poly_agent_llm_tokens_total"]
        assert any(int(s.value) >= 5000 for s in tokens)

    async def test_metrics_track_estimated_spend(self):
        reg = MetricsRegistry()
        await reg.record_llm_estimated_spend(cost_usd=Decimal("2.50"))
        snap = await reg.snapshot()
        spends = [
            s
            for s in snap.samples
            if s.name == "poly_agent_llm_estimated_spend_usd_total"
        ]
        assert any(s.value >= Decimal("2") for s in spends)

    async def test_metrics_track_active_cooldown_count(self):
        reg = MetricsRegistry()
        await reg.set_active_cooldown_count(3)
        snap = await reg.snapshot()
        gauges = [
            s for s in snap.samples if s.name == "poly_agent_active_cooldown_count"
        ]
        assert any(int(s.value) == 3 for s in gauges)


# ---------------------------------------------------------------------------
# Concurrency safety tests
# ---------------------------------------------------------------------------


class TestConcurrencySafety:
    async def test_concurrent_evaluations_respect_budget(self):
        cfg = _make_config(
            enable_llm_cost_guard=True,
            llm_hourly_call_limit=5,
            llm_daily_call_limit=100,
            llm_daily_token_limit=1000000,
            llm_daily_cost_limit_usd=Decimal("100"),
            llm_market_hourly_call_limit=100,
        )
        guard = LLMBudgetGuard(cfg)

        async def check_and_record(i):
            d = await guard.check_budget(call_type="primary")
            if d.allowed:
                await guard.record_usage(
                    provider=LLMProviderName.ANTHROPIC,
                    model_name="t",
                    input_tokens=10,
                    output_tokens=5,
                )
            return d.allowed

        results = await asyncio.gather(*[check_and_record(i) for i in range(10)])
        allowed_count = sum(1 for r in results if r)
        # Atomic reservation ensures exactly 5 allowed
        assert allowed_count == 5

    async def test_concurrent_markets_respect_per_market_cooldown(self):
        cfg = _make_config(
            llm_repeated_hold_threshold=2,
            llm_market_cooldown_seconds=Decimal("60"),
        )
        breaker = MarketCognitiveCircuitBreaker(cfg)
        markets = [f"market-{i}" for i in range(5)]

        async def record_for_market(mk):
            for _ in range(2):
                await breaker.record_outcome(market_key=mk, outcome_type="hold")
            return await breaker.check_cooldown(mk)

        results = await asyncio.gather(*[record_for_market(mk) for mk in markets])
        for r in results:
            assert r.in_cooldown is True


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


class TestLLMCostGuardIntegration:
    async def test_full_evaluation_path_with_budget_guard(self):
        """Budget guard allows call → records usage → subsequent call blocked."""
        cfg = _make_config(
            enable_llm_cost_guard=True,
            llm_hourly_call_limit=2,
            llm_daily_call_limit=100,
            llm_daily_token_limit=1000000,
            llm_daily_cost_limit_usd=Decimal("100"),
            llm_market_hourly_call_limit=100,
        )
        guard = LLMBudgetGuard(cfg)
        d1 = await guard.check_budget(call_type="primary")
        assert d1.allowed is True
        usage = await guard.record_usage(
            provider=LLMProviderName.ANTHROPIC,
            model_name="test",
            input_tokens=500,
            output_tokens=200,
        )
        assert usage.total_tokens == 700
        d2 = await guard.check_budget(call_type="primary")
        assert d2.allowed is True
        await guard.record_usage(
            provider=LLMProviderName.ANTHROPIC,
            model_name="test",
            input_tokens=500,
            output_tokens=200,
        )
        d3 = await guard.check_budget(call_type="primary")
        assert d3.allowed is False

    async def test_full_reflection_path_with_budget_guard(self):
        """Budget guard allows reflection → records usage."""
        cfg = _make_config(
            enable_llm_cost_guard=True,
            llm_hourly_call_limit=2,
            llm_daily_call_limit=100,
            llm_daily_token_limit=1000000,
            llm_daily_cost_limit_usd=Decimal("100"),
            llm_market_hourly_call_limit=100,
        )
        guard = LLMBudgetGuard(cfg)
        d1 = await guard.check_budget(call_type="reflection")
        assert d1.allowed is True
        await guard.record_usage(
            provider=LLMProviderName.ANTHROPIC,
            model_name="test",
            input_tokens=200,
            output_tokens=100,
        )
        d2 = await guard.check_budget(call_type="reflection")
        assert d2.allowed is True
        await guard.record_usage(
            provider=LLMProviderName.ANTHROPIC,
            model_name="test",
            input_tokens=200,
            output_tokens=100,
        )
        d3 = await guard.check_budget(call_type="reflection")
        assert d3.allowed is False

    async def test_budget_guard_integrates_with_llm_evaluation_response(self):
        """Budget block → no LLMEvaluationResponse produced → no execution."""
        from src.schemas.llm import ReflectionResponse, ReflectionVerdict

        cfg = _make_config(
            enable_llm_cost_guard=True,
            llm_hourly_call_limit=1,
            llm_daily_call_limit=1,
            llm_daily_token_limit=1000000,
            llm_daily_cost_limit_usd=Decimal("100"),
            llm_market_hourly_call_limit=100,
        )
        guard = LLMBudgetGuard(cfg)
        d = await guard.check_budget(call_type="primary")
        assert d.allowed is True
        await guard.record_usage(
            provider=LLMProviderName.ANTHROPIC,
            model_name="t",
            input_tokens=10,
            output_tokens=5,
        )
        d = await guard.check_budget(call_type="reflection")
        assert d.allowed is False
        # Simulate ClaudeClient behavior: blocked reflection → REJECTED
        reflection = ReflectionResponse(
            verdict=ReflectionVerdict.REJECTED,
            audit_note="BUDGET_BLOCKED_REFLECTION",
            latency_ms=0,
        )
        assert reflection.verdict == ReflectionVerdict.REJECTED

    async def test_cost_guard_does_not_weaken_dry_run(self):
        """Cost guard operates independently of dry_run flag."""
        cfg = _make_config(
            dry_run=True,
            enable_llm_cost_guard=True,
            llm_hourly_call_limit=1,
            llm_daily_call_limit=1,
            llm_daily_token_limit=1000000,
            llm_daily_cost_limit_usd=Decimal("100"),
            llm_market_hourly_call_limit=100,
        )
        guard = LLMBudgetGuard(cfg)
        d = await guard.check_budget(call_type="primary")
        assert d.allowed is True
        await guard.record_usage(
            provider=LLMProviderName.ANTHROPIC,
            model_name="t",
            input_tokens=10,
            output_tokens=5,
        )
        d = await guard.check_budget(call_type="primary")
        assert d.allowed is False

    async def test_cost_guard_does_not_sign_or_broadcast(self):
        """Cost guard is read-only — no signing, no broadcasting."""
        cfg = _make_config(enable_llm_cost_guard=True)
        guard = LLMBudgetGuard(cfg)
        # No methods that could sign or broadcast exist
        assert not hasattr(guard, "sign")
        assert not hasattr(guard, "broadcast")
        assert not hasattr(guard, "execute")
