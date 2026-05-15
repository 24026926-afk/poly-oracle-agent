#!/usr/bin/env python3
"""
scripts/run_llm_provider_comparison.py

WI-55 CLI entrypoint — runs bounded LLM provider comparison through the
canonical ClaudeClient via its public ``evaluate_for_backtest`` method,
which exercises the full chain: budget guard, cooldown, primary API call,
retry, JSON extraction, and reflection.

Output constrained to docs/backtests/.  Dry run forced True.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import structlog

from src.backtest_runner import BacktestDataLoader, BacktestRunner
from src.backtesting.provider_comparison import (
    derive_calibration_recommendation,
    derive_readiness_verdict,
    generate_comparison_report,
    validate_report_path,
)
from src.schemas.execution import BacktestConfig
from src.schemas.provider_comparison import (
    LLMProviderCalibrationMetrics,
    LLMProviderComparisonConfig,
    LLMProviderComparisonResult,
    LLMProviderComparisonRun,
    LLMProviderCostMetrics,
    LLMProviderDecisionMetrics,
    LLMProviderLatencyMetrics,
)

logger = structlog.get_logger(__name__)

_ZERO = Decimal("0")


# =============================================================================
# ClaudeClient adapter
# =============================================================================


class _ClaudeClientAdapter:
    """Backtest adapter wrapping canonical ``ClaudeClient``.

    Delegates to ``ClaudeClient.evaluate_for_backtest`` — the public
    WI-55 entry point that exercises budget guard, cooldown, primary
    API call + retry, JSON extraction, and reflection.

    Budget/cooldown checks happen ONCE inside ``evaluate_for_backtest``.
    The adapter does NOT pre-check — no double reservation.
    """

    def __init__(self, claude_client: Any, *, provider_label: str) -> None:
        self._client = claude_client
        self._provider_label = provider_label

        self.json_valid_count: int = 0
        self.json_invalid_count: int = 0
        self._latencies: list[float] = []
        self.total_latency_ms: float = 0.0
        self.total_input_tokens: int = 0
        self.total_output_tokens: int = 0
        self.api_call_count: int = 0
        self.calls_with_missing_usage: int = 0
        self.budget_block_count: int = 0
        self.cooldown_block_count: int = 0

    @property
    def call_count(self) -> int:
        return self.api_call_count

    async def evaluate(self, prompt: str) -> dict[str, Any]:
        """Public evaluate(prompt) -> dict contract for BacktestRunner."""
        import re

        t0 = time.monotonic()

        # Derive a per-file market key from the prompt to avoid shared
        # key suppressing unrelated evaluations.
        mk = "backtest"
        for pattern in (r'"condition_id"\s*:\s*"([^"]+)"',
                         r"'condition_id'\s*:\s*'([^']+)'"):
            m = re.search(pattern, str(prompt))
            if m:
                mk = f"bt-{m.group(1)[:20]}"
                break

        try:
            parsed, usage, block_reason = await self._client.evaluate_for_backtest(
                str(prompt), "backtest", market_key=mk,
            )
        except Exception:
            self.json_invalid_count += 1
            elapsed = (time.monotonic() - t0) * 1000
            self._latencies.append(elapsed)
            self.total_latency_ms += elapsed
            return _FALLBACK

        elapsed = (time.monotonic() - t0) * 1000
        self._latencies.append(elapsed)
        self.total_latency_ms += elapsed

        # Count blocks independently of usage: reflection-budget blocks
        # return a non-null usage but still represent a budget-induced
        # fallback that must be tallied for cost-metric audit.
        if block_reason == "cooldown":
            self.cooldown_block_count += 1
        elif block_reason == "budget":
            self.budget_block_count += 1

        if usage is None:
            self.json_invalid_count += 1
            return _FALLBACK

        self.api_call_count += 1

        inp = usage.get("input")
        out = usage.get("output")
        if inp is not None:
            self.total_input_tokens += int(inp)
        else:
            self.calls_with_missing_usage += 1
        if out is not None:
            self.total_output_tokens += int(out)

        reasoning = str(parsed.get("reasoning_log", ""))
        if reasoning.startswith("Provider call blocked") or reasoning.startswith("Fallback —"):
            self.json_invalid_count += 1
            return parsed

        self.json_valid_count += 1
        return parsed

    @property
    def mean_latency_ms(self) -> float:
        return sum(self._latencies) / len(self._latencies) if self._latencies else 0.0

    @property
    def median_latency_ms(self) -> float:
        s = sorted(self._latencies)
        n = len(s)
        if n == 0:
            return 0.0
        return s[n // 2] if n % 2 == 1 else (s[n // 2 - 1] + s[n // 2]) / 2.0

    @property
    def p95_latency_ms(self) -> float:
        s = sorted(self._latencies)
        return s[min(int(len(s) * 0.95), len(s) - 1)] if s else 0.0


_FALLBACK: dict[str, Any] = {
    "market_context": {
        "condition_id": "0000000000",
        "outcome_evaluated": "YES",
        "best_bid": "0.5", "best_ask": "0.5", "midpoint": "0.5",
    },
    "probabilistic_estimate": {"p_true": "0.5", "p_market": "0.5"},
    "risk_assessment": {
        "liquidity_risk_score": "0.5", "resolution_risk_score": "0.5",
        "information_asymmetry_flag": False,
        "risk_notes": "Adapter-level fallback — provider call failed entirely, meeting minimum character requirements for validation schema.",
    },
    "confidence_score": 0.0,
    "decision_boolean": False,
    "recommended_action": "HOLD",
    "reasoning_log": "Adapter fallback — unable to reach provider, conservative HOLD decision enforced to protect trading integrity.",
}


# =============================================================================
# Provider factory
# =============================================================================


def _build_provider_adapter(provider: str) -> _ClaudeClientAdapter:
    from pydantic import SecretStr

    from src.agents.evaluation.claude_client import ClaudeClient
    from src.core.config import AppConfig

    if provider == "deepseek":
        api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        model = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro")
        base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/anthropic")
        max_tokens = int(os.environ.get("DEEPSEEK_MAX_TOKENS", "4096"))
        max_retries = int(os.environ.get("DEEPSEEK_MAX_RETRIES", "2"))
    elif provider == "anthropic":
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
        base_url = "https://api.anthropic.com"
        max_tokens = int(os.environ.get("ANTHROPIC_MAX_TOKENS", "4096"))
        max_retries = int(os.environ.get("ANTHROPIC_MAX_RETRIES", "2"))
    else:
        raise RuntimeError(f"Unknown provider: {provider}")
    if not api_key:
        raise RuntimeError(f"API key not set for '{provider}'.")

    config = AppConfig(
        anthropic_api_key=SecretStr(api_key), anthropic_model=model,
        anthropic_max_tokens=max_tokens, anthropic_max_retries=max_retries,
        deepseek_api_key=SecretStr(api_key), deepseek_base_url=base_url,
        deepseek_model=model, deepseek_max_tokens=max_tokens,
        deepseek_max_retries=max_retries,
        llm_provider=provider, dry_run=True,
        grok_api_key=SecretStr(""), grok_mocked=True, grok_live_enabled=False,
        # WI-52 budget guard: enable with explicit limits (no zero defaults)
        enable_llm_cost_guard=True,
        llm_hourly_call_limit=120,
        llm_daily_call_limit=500,
        llm_daily_token_limit=500_000,
        llm_daily_cost_limit_usd=Decimal("5.00"),
        llm_market_hourly_call_limit=20,
        llm_repeated_hold_threshold=20,
        llm_repeated_invalid_threshold=5,
        llm_market_cooldown_seconds=Decimal("300"),
    )
    in_q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    out_q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    return _ClaudeClientAdapter(
        ClaudeClient(in_queue=in_q, out_queue=out_q, config=config,
                     db_session_factory=None),
        provider_label=provider,
    )


# =============================================================================
# Metrics
# =============================================================================


def _derive_decision_metrics(
    report: object, adapter: _ClaudeClientAdapter,
) -> LLMProviderDecisionMetrics:
    decisions = getattr(report, "decisions", [])
    gk_pass = sum(1 for d in decisions if getattr(d, "gatekeeper_result", "") == "PASSED")
    gk_fail = len(decisions) - gk_pass
    buy = sum(1 for d in decisions if str(getattr(d, "action", "")).upper() == "BUY")
    hold = sum(1 for d in decisions if str(getattr(d, "action", "")).upper() == "HOLD")
    sell = sum(1 for d in decisions if str(getattr(d, "action", "")).upper() == "SELL")
    skip = len(decisions) - buy - hold - sell

    valid = adapter.json_valid_count
    invalid = adapter.json_invalid_count
    all_calls = valid + invalid

    return LLMProviderDecisionMetrics(
        total_calls=all_calls, valid_json_count=valid, invalid_json_count=invalid,
        json_validity_rate=Decimal(valid) / Decimal(all_calls) if all_calls > 0 else _ZERO,
        gatekeeper_passed=gk_pass, gatekeeper_failed=gk_fail,
        gatekeeper_pass_rate=Decimal(gk_pass) / Decimal(valid) if valid > 0 else _ZERO,
        buy_count=buy, hold_count=hold, skip_count=skip, sell_count=sell,
    )


def _derive_calibration_metrics(report: object) -> LLMProviderCalibrationMetrics:
    decisions = getattr(report, "decisions", [])
    buy_decisions = [
        d for d in decisions
        if getattr(d, "decision", False)
        and str(getattr(d, "action", "")).upper() == "BUY"
    ]
    edges = [Decimal("0.0"), Decimal("0.2"), Decimal("0.4"), Decimal("0.6"), Decimal("0.8"), Decimal("1.0")]
    lows, highs, avgs, rates, cnts = [], [], [], [], []
    for i in range(len(edges) - 1):
        lo, hi = edges[i], edges[i + 1]
        bucket = [d for d in buy_decisions if lo <= getattr(d, "confidence", _ZERO) <= hi]
        lows.append(lo); highs.append(hi); cnts.append(len(bucket))
        if bucket:
            avgs.append(sum((getattr(d, "confidence", _ZERO) for d in bucket), _ZERO) / len(bucket))
            wins = sum(1 for d in bucket if getattr(d, "realized_pnl_usdc", _ZERO) > _ZERO)
            rates.append(Decimal(wins) / Decimal(len(bucket)))
        else:
            avgs.append(_ZERO); rates.append(_ZERO)
    max_dev = _ZERO
    for i in range(len(avgs)):
        if cnts[i] > 0 and rates[i] > _ZERO:
            max_dev = max(max_dev, abs(avgs[i] - rates[i]))
    avg_ev = sum((getattr(d, "ev", _ZERO) for d in buy_decisions), _ZERO) / len(buy_decisions) if buy_decisions else _ZERO
    total_pnl = getattr(report, "net_pnl_usdc", _ZERO)
    total_trades = getattr(report, "total_trades", 0)
    avg_real = total_pnl / Decimal(total_trades) if total_trades > 0 else _ZERO
    ev_dev = abs(avg_ev - avg_real) if total_trades > 0 and avg_ev > _ZERO else _ZERO
    total_decisions = len(decisions)
    outcome_cov = (
        Decimal(total_trades) / Decimal(total_decisions)
        if total_decisions > 0
        else _ZERO
    )
    # has_outcome_data: true when any decision carries an explicit
    # outcome_resolved flag.  Using outcome_resolved (rather than a
    # non-zero PnL check) ensures valid break-even outcomes are still
    # counted as resolved data — a zero-PnL settled trade is real
    # outcome evidence for calibration.
    has_outcomes = any(
        bool(getattr(d, "outcome_resolved", False))
        for d in decisions
    )

    return LLMProviderCalibrationMetrics(
        confidence_bucket_low=lows, confidence_bucket_high=highs,
        confidence_bucket_avg=avgs, confidence_bucket_observed_win_rate=rates,
        confidence_bucket_count=cnts, confidence_calibration_deviation=max_dev,
        avg_ev=avg_ev, avg_realized_return=avg_real,
        ev_calibration_deviation=ev_dev, outcome_coverage_fraction=outcome_cov,
        has_outcome_data=has_outcomes,
    )


def _derive_cost_metrics(
    adapter: _ClaudeClientAdapter, config: LLMProviderComparisonConfig,
) -> LLMProviderCostMetrics:
    api_calls = adapter.api_call_count
    missing = adapter.calls_with_missing_usage
    has_usage = adapter.total_input_tokens > 0 or adapter.total_output_tokens > 0
    if api_calls == 0:
        return LLMProviderCostMetrics(
            total_input_tokens=0, total_output_tokens=0, total_tokens=0,
            total_estimated_cost_usd=_ZERO, is_estimated=False,
            budget_block_count=adapter.budget_block_count,
            cooldown_block_count=adapter.cooldown_block_count,
            calls_with_missing_usage=0,
        )
    if has_usage:
        est_in = adapter.total_input_tokens + (missing * config.fallback_tokens_per_call) if missing > 0 else adapter.total_input_tokens
        est_out = adapter.total_output_tokens + (missing * max(config.fallback_tokens_per_call // 4, 256)) if missing > 0 else adapter.total_output_tokens
        est_total = est_in + est_out
        est_cost = Decimal(est_in) * config.cost_per_input_token_usd + Decimal(est_out) * config.cost_per_output_token_usd
        return LLMProviderCostMetrics(
            total_input_tokens=est_in, total_output_tokens=est_out, total_tokens=est_total,
            total_estimated_cost_usd=est_cost, is_estimated=(missing > 0),
            budget_block_count=adapter.budget_block_count,
            cooldown_block_count=adapter.cooldown_block_count,
            calls_with_missing_usage=missing,
        )
    est_in = api_calls * config.fallback_tokens_per_call
    est_out = api_calls * max(config.fallback_tokens_per_call // 4, 256)
    est_total = est_in + est_out
    est_cost = Decimal(est_in) * config.cost_per_input_token_usd + Decimal(est_out) * config.cost_per_output_token_usd
    return LLMProviderCostMetrics(
        total_input_tokens=est_in, total_output_tokens=est_out, total_tokens=est_total,
        total_estimated_cost_usd=est_cost, is_estimated=True,
        budget_block_count=adapter.budget_block_count,
        cooldown_block_count=adapter.cooldown_block_count,
        calls_with_missing_usage=api_calls,
    )


def _derive_latency_metrics(adapter: _ClaudeClientAdapter) -> LLMProviderLatencyMetrics:
    lats = adapter._latencies
    return LLMProviderLatencyMetrics(
        min_latency_ms=min(lats) if lats else 0.0, max_latency_ms=max(lats) if lats else 0.0,
        mean_latency_ms=float(adapter.mean_latency_ms),
        median_latency_ms=float(adapter.median_latency_ms),
        p95_latency_ms=float(adapter.p95_latency_ms),
        p99_latency_ms=0.0, sample_count=len(lats),
    )


# =============================================================================
# Single-provider backtest
# =============================================================================


async def _run_provider_backtest(
    *, adapter: _ClaudeClientAdapter, comp_config: LLMProviderComparisonConfig,
    provider_name: str, model_name: str, data_dir: str,
) -> LLMProviderComparisonResult:
    bt_cfg = BacktestConfig(data_dir=data_dir, initial_bankroll_usdc=comp_config.initial_bankroll_usdc, dry_run=True)
    runner = BacktestRunner(config=bt_cfg, data_loader=BacktestDataLoader(bt_cfg), claude_client=adapter)
    report = await runner.run()
    decision = _derive_decision_metrics(report, adapter)
    calibration = _derive_calibration_metrics(report)
    cost = _derive_cost_metrics(adapter, comp_config)
    latency = _derive_latency_metrics(adapter)
    verdict, reasons = derive_readiness_verdict(
        decision_metrics=decision, calibration_metrics=calibration,
        cost_metrics=cost, latency_metrics=latency,
        baseline_cost_usd=comp_config.anthropic_baseline_cost_usd,
        baseline_mean_latency_ms=comp_config.anthropic_baseline_latency_ms,
        config=comp_config,
    )
    rec = derive_calibration_recommendation(
        provider=provider_name, model_name=model_name,
        calibration_metrics=calibration, decision_metrics=decision, config=comp_config,
    )
    return LLMProviderComparisonResult(
        provider=provider_name, model_name=model_name,
        decision_metrics=decision, calibration_metrics=calibration,
        cost_metrics=cost, latency_metrics=latency,
        readiness_verdict=verdict, readiness_reasons=reasons,
        calibration_recommendation=rec,
    )


# =============================================================================
# CLI
# =============================================================================


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="WI-55 LLM provider comparison runner")
    p.add_argument("--data-dir", required=True)
    p.add_argument("--config", required=False)
    p.add_argument("--output", required=True)
    return p.parse_args()


def _load_overrides(path: str) -> dict:
    raw = Path(path).read_text(encoding="utf-8")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    try:
        import yaml
        loaded = yaml.safe_load(raw)
        if loaded is None:
            return {}
        if not isinstance(loaded, dict):
            raise ValueError("Config must be a dict")
        return loaded
    except ImportError:
        raise ValueError("Config is not JSON and PyYAML is not installed")


async def main() -> int:
    args = parse_args()
    overrides = _load_overrides(args.config) if args.config else {}
    cfg = {"data_dir": args.data_dir, "dry_run": True}
    cfg.update(overrides)
    comp_config = LLMProviderComparisonConfig(**cfg)
    output_path = Path(args.output)
    try:
        validate_report_path(output_path)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    started = datetime.now(timezone.utc)
    results: list[LLMProviderComparisonResult] = []
    error_msg: str | None = None

    # --- DeepSeek ---
    try:
        ds = _build_provider_adapter("deepseek")
    except RuntimeError as exc:
        error_msg = f"deepseek: {exc}"
        run = LLMProviderComparisonRun(config=comp_config, started_at_utc=started,
                                        completed_at_utc=datetime.now(timezone.utc),
                                        error_message=error_msg)
        generate_comparison_report(run=run, output_path=output_path)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    try:
        results.append(await _run_provider_backtest(
            adapter=ds, comp_config=comp_config, provider_name="deepseek",
            model_name=comp_config.deepseek_model, data_dir=comp_config.data_dir,
        ))
    except Exception as exc:
        error_msg = f"deepseek backtest: {exc}"
        run = LLMProviderComparisonRun(config=comp_config, started_at_utc=started,
                                        completed_at_utc=datetime.now(timezone.utc),
                                        error_message=error_msg)
        generate_comparison_report(run=run, output_path=output_path)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    # --- Anthropic sampling (fraction-gated) ---
    if comp_config.enable_anthropic_sampling and comp_config.anthropic_sample_fraction > _ZERO:
        try:
            an = _build_provider_adapter("anthropic")
        except RuntimeError as exc:
            logger.warning("anthropic.adapter_skipped", error=str(exc))
        else:
            an_dir = comp_config.data_dir
            frac = float(comp_config.anthropic_sample_fraction)
            tmpdir: str | None = None
            try:
                all_files = sorted(Path(comp_config.data_dir).glob("*.json"))
                if all_files and 0 < frac < 1:
                    n = max(1, int(len(all_files) * frac))
                    sampled = random.sample(all_files, n)
                    tmpdir = tempfile.mkdtemp(prefix="wi55_an_")
                    for f in sampled:
                        shutil.copy2(f, tmpdir)
                    an_dir = tmpdir
                results.append(await _run_provider_backtest(
                    adapter=an, comp_config=comp_config, provider_name="anthropic",
                    model_name=comp_config.anthropic_model, data_dir=an_dir,
                ))
            except Exception as exc:
                logger.error("anthropic.backtest_failed", error=str(exc))
            finally:
                if tmpdir is not None:
                    shutil.rmtree(tmpdir, ignore_errors=True)

    # --- Report ---
    run = LLMProviderComparisonRun(config=comp_config, started_at_utc=started,
                                    completed_at_utc=datetime.now(timezone.utc),
                                    results=results, error_message=error_msg)
    generate_comparison_report(run=run, output_path=output_path)
    for r in results:
        print(
            f"Comparison: provider={r.provider} verdict={r.readiness_verdict.value} "
            f"json={r.decision_metrics.valid_json_count}/{r.decision_metrics.total_calls} "
            f"cost={r.cost_metrics.total_estimated_cost_usd} USD "
            f"lat={r.latency_metrics.mean_latency_ms:.0f}ms "
            f"budget_blocks={r.cost_metrics.budget_block_count} "
            f"cooldown_blocks={r.cost_metrics.cooldown_block_count}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
