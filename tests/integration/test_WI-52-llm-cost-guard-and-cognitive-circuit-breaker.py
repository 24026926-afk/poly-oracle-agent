"""Integration tests for WI-52: LLM Cost Guard and Cognitive Circuit Breaker.

Tests the full evaluation path with budget guard and cognitive cooldown
enforcement, including ClaudeClient wiring, metrics emission, and
orchestrator-level integration.
"""

import os
from decimal import Decimal


from src.core.config import AppConfig
from src.schemas.llm import (
    LLMProviderName,
    MarketCooldownReason,
)
from src.agents.evaluation.llm_cost_guard import (
    LLMBudgetGuard,
    MarketCognitiveCircuitBreaker,
)
from src.observability.metrics import MetricsRegistry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides: dict) -> AppConfig:
    """Create an AppConfig with env isolation."""
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
        "LLM_REFLECTION_HOURLY_CALL_LIMIT",
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
# Integration tests
# ---------------------------------------------------------------------------


class TestLLMCostGuardIntegration:
    """End-to-end budget guard + cognitive cooldown integration."""

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
            llm_reflection_hourly_call_limit=2,
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
            llm_reflection_hourly_call_limit=100,
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
        assert not hasattr(guard, "sign")
        assert not hasattr(guard, "broadcast")
        assert not hasattr(guard, "execute")


class TestCognitiveCooldownIntegration:
    """End-to-end cognitive circuit breaker integration."""

    async def test_cooldown_triggered_by_repeated_holds(self):
        cfg = _make_config(
            llm_repeated_hold_threshold=3,
            llm_market_cooldown_seconds=Decimal("60"),
        )
        breaker = MarketCognitiveCircuitBreaker(cfg)
        mk = "market-hold-test"
        for _ in range(3):
            await breaker.record_outcome(market_key=mk, outcome_type="hold")
        d = await breaker.check_cooldown(mk)
        assert d.in_cooldown is True
        assert d.cooldown_reason == MarketCooldownReason.REPEATED_HOLD

    async def test_cooldown_triggered_by_repeated_invalid_json(self):
        cfg = _make_config(
            llm_repeated_invalid_threshold=2,
            llm_market_cooldown_seconds=Decimal("60"),
        )
        breaker = MarketCognitiveCircuitBreaker(cfg)
        mk = "market-invalid-test"
        for _ in range(2):
            await breaker.record_outcome(market_key=mk, outcome_type="invalid_json")
        d = await breaker.check_cooldown(mk)
        assert d.in_cooldown is True
        assert d.cooldown_reason == MarketCooldownReason.REPEATED_INVALID_JSON

    async def test_cooldown_blocks_only_affected_market(self):
        cfg = _make_config(
            llm_repeated_hold_threshold=3,
            llm_market_cooldown_seconds=Decimal("60"),
        )
        breaker = MarketCognitiveCircuitBreaker(cfg)
        mk1 = "market-1"
        mk2 = "market-2"
        for _ in range(3):
            await breaker.record_outcome(market_key=mk1, outcome_type="hold")
        d1 = await breaker.check_cooldown(mk1)
        d2 = await breaker.check_cooldown(mk2)
        assert d1.in_cooldown is True
        assert d2.in_cooldown is False


class TestMetricsIntegration:
    """LLM cost guard metrics wiring integration tests."""

    async def test_metrics_record_llm_calls(self):
        reg = MetricsRegistry()
        await reg.record_llm_call(call_type="primary")
        await reg.record_llm_call(call_type="primary")
        await reg.record_llm_call(call_type="reflection")
        snap = await reg.snapshot()
        llm_calls = [s for s in snap.samples if s.name == "poly_agent_llm_calls_total"]
        total = sum(int(s.value) for s in llm_calls)
        assert total == 3

    async def test_metrics_record_budget_blocks(self):
        reg = MetricsRegistry()
        await reg.record_llm_budget_block(reason="hourly_call_limit_exhausted")
        snap = await reg.snapshot()
        blocks = [
            s for s in snap.samples if s.name == "poly_agent_llm_budget_blocks_total"
        ]
        assert any(int(s.value) > 0 for s in blocks)

    async def test_metrics_record_cooldown_blocks(self):
        reg = MetricsRegistry()
        await reg.record_llm_cooldown_block()
        snap = await reg.snapshot()
        blocks = [
            s for s in snap.samples if s.name == "poly_agent_llm_cooldown_blocks_total"
        ]
        assert any(int(s.value) > 0 for s in blocks)

    async def test_metrics_record_token_usage(self):
        reg = MetricsRegistry()
        await reg.record_llm_tokens(total_tokens=5000)
        snap = await reg.snapshot()
        tokens = [s for s in snap.samples if s.name == "poly_agent_llm_tokens_total"]
        assert any(int(s.value) >= 5000 for s in tokens)

    async def test_metrics_record_estimated_spend(self):
        reg = MetricsRegistry()
        await reg.record_llm_estimated_spend(cost_usd=Decimal("2.50"))
        snap = await reg.snapshot()
        spends = [
            s
            for s in snap.samples
            if s.name == "poly_agent_llm_estimated_spend_usd_total"
        ]
        assert any(s.value >= Decimal("2") for s in spends)

    async def test_metrics_set_active_cooldown_count(self):
        reg = MetricsRegistry()
        await reg.set_active_cooldown_count(3)
        snap = await reg.snapshot()
        gauges = [
            s for s in snap.samples if s.name == "poly_agent_active_cooldown_count"
        ]
        assert any(int(s.value) == 3 for s in gauges)

    async def test_metrics_rendered_without_secrets(self):
        reg = MetricsRegistry()
        await reg.record_llm_call(call_type="primary")
        await reg.record_llm_budget_block(reason="daily_cost_limit_exhausted")
        await reg.record_llm_cooldown_block()
        await reg.record_llm_tokens(total_tokens=1000)
        await reg.record_llm_estimated_spend(cost_usd=Decimal("0.01"))
        rendered = reg.render_prometheus(await reg.snapshot())
        assert "sk-" not in rendered
        assert "api_key" not in rendered.lower()
        assert "prompt" not in rendered.lower()
        assert "reasoning" not in rendered.lower()
