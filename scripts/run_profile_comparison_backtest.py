#!/usr/bin/env python3
"""
scripts/run_profile_comparison_backtest.py

Real-LLM risk-profile comparison backtest (WI-67 follow-on).

Drives the canonical ``ClaudeClient.evaluate_for_backtest`` (DeepSeek) over a
WI-43 historical dataset, then applies TWO terminal-Gatekeeper risk profiles
(conservative vs aggressive) to the SAME LLM candidate — so the only variable is
the confidence gate. Realized PnL is computed from each market's resolved
outcome (the dataset is lookahead-safe: the LLM never sees the outcome).

This bypasses the stubbed BacktestRunner (which uses a fallback HOLD evaluator)
and the WI-55 provider-comparison tool (which compares providers, not profiles).

Usage:
    python scripts/run_profile_comparison_backtest.py \\
        --data-dir data/historical_sample \\
        --aggressive-confidence 0.65 --limit 80 \\
        --output docs/backtests/profile_comparison.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

import httpx
import structlog
from pydantic import BaseModel, ConfigDict, field_validator

from src.agents.context.prompt_factory import PromptFactory
from src.agents.evaluation.claude_client import ClaudeClient
from src.core.config import get_config
from src.schemas.llm import LLMEvaluationResponse, RecommendedAction

logger = structlog.get_logger(__name__)

_BANKROLL = Decimal("1000")
_QUESTION_FETCH_TIMEOUT_S = 10.0
_QUESTION_FETCH_RETRIES = 2


# ---------------------------------------------------------------------------
# WI-68: enrichment diagnostic — typed, Decimal-native models
# ---------------------------------------------------------------------------


class EnrichmentArmStatus(str, Enum):
    """Typed outcome of sourcing the market question for the enriched arm."""

    ENRICHED = "ENRICHED"
    SKIPPED_NO_QUESTION = "SKIPPED_NO_QUESTION"


class EnrichmentVerdict(str, Enum):
    """The WI-68 conclusion the diagnostic must produce and persist.

    - PROMPT_STARVATION (a): enriched p_true diverges from the midpoint beyond
      both the baseline arm and the materiality threshold → alpha was discarded
      at the prompt layer; the production prompt fix is itself a result.
    - LLM_WEAK (b): enriched p_true still tracks the midpoint → the LLM alone is
      weak; WI-71's external-data path is justified.
    - INSUFFICIENT_DATA: no enriched arm produced (question never sourced).
    """

    PROMPT_STARVATION = "PROMPT_STARVATION"
    LLM_WEAK = "LLM_WEAK"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class QuestionFetchResult(BaseModel):
    """Typed result of the lookahead-safe Gamma question fetch (fail-closed)."""

    model_config = ConfigDict(frozen=True)

    status: EnrichmentArmStatus
    question: str | None = None


class EnrichmentDeltaRecord(BaseModel):
    """One snapshot's |p_true - midpoint| for the baseline vs enriched arm.

    All probability/delta fields are ``Decimal`` and reject raw ``float``.
    """

    model_config = ConfigDict(frozen=True)

    token_id: str
    condition_id: str
    midpoint: Decimal
    p_true_baseline: Decimal
    p_true_enriched: Decimal | None = None
    delta_baseline: Decimal
    delta_enriched: Decimal | None = None
    question_present: bool

    @field_validator(
        "midpoint",
        "p_true_baseline",
        "p_true_enriched",
        "delta_baseline",
        "delta_enriched",
        mode="before",
    )
    @classmethod
    def _reject_float(cls, v: object) -> Decimal | None:
        if v is None:
            return None
        if isinstance(v, float):
            raise ValueError(
                "WI-68: probability/delta fields must be Decimal, never float"
            )
        if isinstance(v, Decimal):
            return v
        return Decimal(str(v))

    @classmethod
    def build(
        cls,
        *,
        token_id: str,
        condition_id: str,
        midpoint: Decimal,
        p_true_baseline: Decimal,
        p_true_enriched: Decimal | None = None,
    ) -> "EnrichmentDeltaRecord":
        """Compute the absolute deltas in Decimal; enriched fields are None when
        the question could not be sourced (typed skip of the enriched arm)."""
        delta_baseline = abs(p_true_baseline - midpoint)
        if p_true_enriched is None:
            return cls(
                token_id=token_id,
                condition_id=condition_id,
                midpoint=midpoint,
                p_true_baseline=p_true_baseline,
                p_true_enriched=None,
                delta_baseline=delta_baseline,
                delta_enriched=None,
                question_present=False,
            )
        return cls(
            token_id=token_id,
            condition_id=condition_id,
            midpoint=midpoint,
            p_true_baseline=p_true_baseline,
            p_true_enriched=p_true_enriched,
            delta_baseline=delta_baseline,
            delta_enriched=abs(p_true_enriched - midpoint),
            question_present=True,
        )


class EnrichmentDiagnosticReport(BaseModel):
    """Per-snapshot + aggregate enriched-vs-baseline delta distribution, plus the
    typed WI-68 verdict (Decimal-native)."""

    model_config = ConfigDict(frozen=True)

    record_count: int
    materiality_threshold: Decimal
    baseline_delta_mean: Decimal
    baseline_delta_median: Decimal
    baseline_delta_max: Decimal
    enriched_count: int
    enriched_delta_mean: Decimal
    enriched_delta_median: Decimal
    enriched_delta_max: Decimal
    enriched_above_threshold: int
    enriched_skips: int
    verdict: EnrichmentVerdict
    verdict_summary: str
    # Per-snapshot rows persisted alongside the aggregate (WI-68 DoD).
    records: list[EnrichmentDeltaRecord]

    @classmethod
    def from_records(
        cls,
        records: list[EnrichmentDeltaRecord],
        *,
        materiality_threshold: Decimal,
    ) -> "EnrichmentDiagnosticReport":
        base = [r.delta_baseline for r in records]
        enr = [r.delta_enriched for r in records if r.delta_enriched is not None]
        baseline_mean = _decimal_mean(base)
        enriched_mean = _decimal_mean(enr)
        verdict, summary = cls._derive_verdict(
            enriched=enr,
            enriched_mean=enriched_mean,
            baseline_mean=baseline_mean,
            materiality_threshold=materiality_threshold,
        )
        return cls(
            record_count=len(records),
            materiality_threshold=materiality_threshold,
            baseline_delta_mean=baseline_mean,
            baseline_delta_median=_decimal_median(base),
            baseline_delta_max=max(base) if base else Decimal("0"),
            enriched_count=len(enr),
            enriched_delta_mean=enriched_mean,
            enriched_delta_median=_decimal_median(enr),
            enriched_delta_max=max(enr) if enr else Decimal("0"),
            enriched_above_threshold=sum(1 for d in enr if d > materiality_threshold),
            enriched_skips=sum(1 for r in records if r.delta_enriched is None),
            verdict=verdict,
            verdict_summary=summary,
            records=list(records),
        )

    @staticmethod
    def _derive_verdict(
        *,
        enriched: list[Decimal],
        enriched_mean: Decimal,
        baseline_mean: Decimal,
        materiality_threshold: Decimal,
    ) -> tuple[EnrichmentVerdict, str]:
        if not enriched:
            return (
                EnrichmentVerdict.INSUFFICIENT_DATA,
                "No enriched arm produced; the market question could not be "
                "sourced, so the prompt-starvation confound is unresolved.",
            )
        if enriched_mean > materiality_threshold and enriched_mean > baseline_mean:
            return (
                EnrichmentVerdict.PROMPT_STARVATION,
                "(a) Enriched p_true diverges from the midpoint beyond both the "
                "baseline arm and the materiality threshold — alpha was being "
                "discarded at the prompt layer. The production prompt fix is a "
                "result; per-category edge analysis (WI-70) is warranted.",
            )
        return (
            EnrichmentVerdict.LLM_WEAK,
            "(b) Enriched p_true still tracks the midpoint (no material divergence "
            "beyond baseline) — the LLM alone is weak; WI-71's external-data path "
            "is justified.",
        )


def _decimal_mean(xs: list[Decimal]) -> Decimal:
    return (sum(xs, Decimal("0")) / Decimal(len(xs))) if xs else Decimal("0")


def _decimal_median(xs: list[Decimal]) -> Decimal:
    if not xs:
        return Decimal("0")
    ordered = sorted(xs)
    n = len(ordered)
    mid = n // 2
    if n % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / Decimal("2")


def _cache_key(token: str, timestamp: str, arm: str) -> str:
    """Candidate cache key. The arm is part of the key so a baseline candidate
    is never cross-served as an enriched one (and vice versa)."""
    return f"{token}|{timestamp}|{arm}"


def _p_true(parsed: dict[str, Any] | None) -> Decimal | None:
    """Extract the LLM's p_true as Decimal; None if absent/unparseable."""
    if not isinstance(parsed, dict):
        return None
    estimate = parsed.get("probabilistic_estimate")
    if not isinstance(estimate, dict):
        return None
    raw = estimate.get("p_true")
    if raw is None:
        return None
    try:
        return Decimal(str(raw))
    except Exception:
        return None


async def _fetch_question(
    condition_id: str,
    http: Any,
    base_url: str,
    *,
    retries: int = _QUESTION_FETCH_RETRIES,
    timeout: float = _QUESTION_FETCH_TIMEOUT_S,
) -> QuestionFetchResult:
    """Fetch a market question by condition_id (lookahead-safe static metadata).

    Explicit timeout + bounded retry; fails closed to a typed SKIPPED result so
    the enriched arm is dropped (never fabricated, never a silent fallthrough).
    A definitive 200 with no question is not retried.
    """
    url = f"{base_url.rstrip('/')}/markets/{condition_id}"
    for attempt in range(retries + 1):
        try:
            resp = await http.get(url, timeout=httpx.Timeout(timeout))
            if resp.status_code == 200:
                data = resp.json() or {}
                question = str(data.get("question", "")).strip()
                if question:
                    return QuestionFetchResult(
                        status=EnrichmentArmStatus.ENRICHED, question=question
                    )
                return QuestionFetchResult(
                    status=EnrichmentArmStatus.SKIPPED_NO_QUESTION, question=None
                )
        except Exception as exc:  # noqa: BLE001 — fail closed, bounded retry
            logger.warning(
                "wi68.question_fetch_failed",
                condition_id=condition_id,
                attempt=attempt,
                error=str(exc),
            )
    return QuestionFetchResult(
        status=EnrichmentArmStatus.SKIPPED_NO_QUESTION, question=None
    )


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Real-LLM risk-profile comparison backtest")
    p.add_argument("--data-dir", required=True, help="WI-43 dataset directory")
    p.add_argument("--conservative-confidence", type=str, default="0.75")
    p.add_argument("--conservative-ev", type=str, default="0.02")
    p.add_argument("--aggressive-confidence", type=str, default="0.65")
    p.add_argument("--aggressive-ev", type=str, default="0.005")
    p.add_argument(
        "--limit", type=int, default=0, help="Max snapshots to evaluate (0 = all)"
    )
    p.add_argument("--output", default="docs/backtests/profile_comparison.json")
    p.add_argument(
        "--enrichment-output",
        default="docs/backtests/wi68_enrichment_diagnostic.json",
        help="WI-68 enriched-vs-baseline delta diagnostic report",
    )
    p.add_argument(
        "--materiality-threshold",
        type=str,
        default="0.05",
        help="|p_true - midpoint| above which the question moved the estimate",
    )
    p.add_argument(
        "--cache",
        default="docs/backtests/_candidates_cache.jsonl",
        help="JSONL cache of LLM candidates so re-runs skip the DeepSeek calls",
    )
    return p.parse_args()


def _load_dataset(data_dir: Path) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Return (snapshots, outcomes_by_token). Outcomes are kept separate from
    snapshots so the LLM prompt never sees the resolution (no lookahead)."""
    snapshots: list[dict[str, Any]] = []
    outcomes: dict[str, str] = {}
    for path in sorted(data_dir.glob("*.json")):
        if path.name == "manifest.json":
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        if path.name.endswith("_outcomes.json"):
            tok = str(data.get("token_id", ""))
            outcome = data.get("resolved_outcome")
            if tok and outcome:
                outcomes[tok] = str(outcome).strip().upper()
            continue
        if isinstance(data, list):
            snapshots.extend(s for s in data if isinstance(s, dict))
    snapshots.sort(key=lambda s: str(s.get("timestamp_utc", "")))
    return snapshots, outcomes


def _profile_context(min_confidence: str, min_ev: str) -> dict[str, float]:
    """Gatekeeper validation context — confidence and EV gates vary by profile;
    the remaining knobs fall back to their conservative module defaults.

    The TTR gate is neutralized: the backtest evaluates already-resolved markets,
    so ``hours_to_resolution`` computed against "now" is always negative and would
    block every candidate. TTR is orthogonal to the confidence/EV question.
    """
    return {
        "min_confidence": float(Decimal(min_confidence)),
        "min_ev_threshold": float(Decimal(min_ev)),
        "min_ttr_hours": -1.0e12,
    }


def _authoritative_candidate(
    parsed: dict[str, Any], snap: dict[str, Any]
) -> dict[str, Any]:
    """Override the LLM-echoed market facts with authoritative snapshot data
    before gatekeeping (mirrors WI-65). The LLM owns p_true/confidence only."""
    candidate = dict(parsed)
    candidate["market_context"] = {
        "condition_id": snap.get("condition_id", "0x0"),
        "outcome_evaluated": "YES",
        "best_bid": str(snap["best_bid"]),
        "best_ask": str(snap["best_ask"]),
        "midpoint": str(snap["midpoint"]),
        "market_end_date": snap.get("market_end_date"),
    }
    pe = dict(candidate.get("probabilistic_estimate") or {})
    pe["p_market"] = str(snap["midpoint"])
    candidate["probabilistic_estimate"] = pe
    return candidate


def _realized_pnl(
    *, position_size_pct: Decimal, entry_midpoint: Decimal, outcome: str
) -> Decimal:
    """PnL of a BUY-YES position settled at resolution. Bought at the midpoint;
    YES settles at 1, NO settles at 0."""
    if entry_midpoint <= Decimal("0") or entry_midpoint >= Decimal("1"):
        return Decimal("0")
    stake = position_size_pct * _BANKROLL
    if outcome == "YES":
        return stake * (Decimal("1") - entry_midpoint) / entry_midpoint
    return -stake


class _ProfileTally:
    def __init__(self, label: str, min_confidence: str, min_ev: str) -> None:
        self.label = label
        self.min_confidence = min_confidence
        self.min_ev = min_ev
        self.context = _profile_context(min_confidence, min_ev)
        self.trades = 0
        self.wins = 0
        self.resolved_trades = 0
        self.net_pnl = Decimal("0")
        self.confidence_sum = Decimal("0")
        self.ev_sum = Decimal("0")

    def apply(
        self, parsed: dict[str, Any], snap: dict[str, Any], outcome: str | None
    ) -> None:
        try:
            resp = LLMEvaluationResponse.model_validate(
                _authoritative_candidate(parsed, snap), context=self.context
            )
        except Exception:
            return  # candidate could not be gatekept under this profile — skip
        if resp.recommended_action != RecommendedAction.BUY:
            return
        self.trades += 1
        self.confidence_sum += Decimal(str(resp.confidence_score))
        self.ev_sum += Decimal(str(resp.expected_value))
        if outcome in ("YES", "NO"):
            self.resolved_trades += 1
            pnl = _realized_pnl(
                position_size_pct=Decimal(str(resp.position_size_pct)),
                entry_midpoint=Decimal(str(snap["midpoint"])),
                outcome=outcome,
            )
            self.net_pnl += pnl
            if pnl > 0:
                self.wins += 1

    def summary(self) -> dict[str, Any]:
        avg_conf = self.confidence_sum / self.trades if self.trades else Decimal("0")
        avg_ev = self.ev_sum / self.trades if self.trades else Decimal("0")
        win_rate = (
            Decimal(self.wins) / Decimal(self.resolved_trades)
            if self.resolved_trades
            else Decimal("0")
        )
        return {
            "label": self.label,
            "min_confidence": self.min_confidence,
            "min_ev_threshold": self.min_ev,
            "accepted_trades": self.trades,
            "resolved_trades": self.resolved_trades,
            "wins": self.wins,
            "win_rate": str(round(win_rate, 4)),
            "net_pnl_usdc": str(round(self.net_pnl, 4)),
            "avg_confidence": str(round(avg_conf, 4)),
            "avg_ev": str(round(avg_ev, 6)),
        }


async def main() -> int:
    args = _parse_args()
    data_dir = Path(args.data_dir)
    snapshots, outcomes = _load_dataset(data_dir)
    if args.limit > 0:
        snapshots = snapshots[: args.limit]
    if not snapshots:
        print("No snapshots found.", file=sys.stderr)
        return 1

    config = get_config()
    client = ClaudeClient(
        in_queue=asyncio.Queue(),
        out_queue=asyncio.Queue(),
        config=config,
        db_session_factory=None,
    )

    conservative = _ProfileTally(
        "conservative", args.conservative_confidence, args.conservative_ev
    )
    aggressive = _ProfileTally(
        "aggressive", args.aggressive_confidence, args.aggressive_ev
    )

    # Candidate cache: LLM evals are the only cost; gatekeeping is free. Cache
    # the raw candidate per snapshot so re-runs with different thresholds skip
    # DeepSeek entirely.
    cache_path = Path(args.cache)
    cache: dict[str, dict[str, Any]] = {}
    if cache_path.exists():
        for line in cache_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rec = json.loads(line)
                cache[rec["key"]] = rec["parsed"]
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_fh = cache_path.open("a", encoding="utf-8")

    evaluated = 0
    eval_errors = 0
    cache_hits = 0
    records: list[EnrichmentDeltaRecord] = []
    logger.info(
        "profile_backtest.start",
        snapshots=len(snapshots),
        markets=len(outcomes),
        provider=config.llm_provider,
        cached=len(cache),
    )

    async def _eval_arm(
        idx: int, snap: dict[str, Any], token: str, arm: str, question: str | None
    ) -> dict[str, Any] | None:
        """Evaluate one arm (baseline=no question, enriched=question). The cache
        key includes the arm so a baseline candidate is never cross-served as an
        enriched one. Returns the parsed candidate, or None on eval error."""
        nonlocal cache_hits, eval_errors
        key = _cache_key(token, str(snap.get("timestamp_utc", "")), arm)
        cached = cache.get(key)
        if cached is not None:
            cache_hits += 1
            return cached
        market_state: dict[str, Any] = {
            "condition_id": snap.get("condition_id", "Unknown"),
            "best_bid": float(snap["best_bid"]),
            "best_ask": float(snap["best_ask"]),
            "midpoint": float(snap["midpoint"]),
            "spread": float(snap.get("spread", 0.0)),
            "timestamp": snap.get("timestamp_utc", ""),
        }
        if question:
            market_state["question"] = question
        prompt = PromptFactory.build_evaluation_prompt(market_state)
        try:
            parsed, _usage, _block = await client.evaluate_for_backtest(
                prompt,
                snapshot_id=f"bt-{arm}-{idx}",
                market_key=f"bt-{arm}-{token}-{idx}",  # unique → no cooldown
            )
        except Exception as exc:
            eval_errors += 1
            logger.warning(
                "profile_backtest.eval_failed", idx=idx, arm=arm, error=str(exc)
            )
            return None
        cache[key] = parsed
        cache_fh.write(json.dumps({"key": key, "parsed": parsed}, default=str) + "\n")
        cache_fh.flush()
        return parsed

    async with httpx.AsyncClient() as http:
        for idx, snap in enumerate(snapshots):
            token = str(snap.get("token_id", ""))
            outcome = outcomes.get(token)
            condition_id = str(snap.get("condition_id", "Unknown"))

            # Enriched arm: source the question lookahead-safely (static metadata).
            fetched = await _fetch_question(condition_id, http, config.gamma_api_url)
            question = fetched.question

            baseline = await _eval_arm(idx, snap, token, "baseline", None)
            if baseline is None:
                continue
            enriched = (
                await _eval_arm(idx, snap, token, "enriched", question)
                if question
                else None
            )

            p_base = _p_true(baseline)
            if p_base is None:
                continue
            midpoint = Decimal(str(snap["midpoint"]))
            p_enr = _p_true(enriched) if enriched is not None else None
            records.append(
                EnrichmentDeltaRecord.build(
                    token_id=token,
                    condition_id=condition_id,
                    midpoint=midpoint,
                    p_true_baseline=p_base,
                    p_true_enriched=p_enr,
                )
            )

            evaluated += 1
            # Profile tallies are fed by the enriched (production-representative)
            # candidate when available, else the baseline.
            candidate = enriched if enriched is not None else baseline
            conservative.apply(candidate, snap, outcome)
            aggressive.apply(candidate, snap, outcome)

            if (idx + 1) % 25 == 0:
                logger.info(
                    "profile_backtest.progress",
                    done=idx + 1,
                    total=len(snapshots),
                    cons_trades=conservative.trades,
                    aggr_trades=aggressive.trades,
                )

    cache_fh.close()

    diagnostic = EnrichmentDiagnosticReport.from_records(
        records, materiality_threshold=Decimal(args.materiality_threshold)
    )

    report = {
        "data_dir": str(data_dir),
        "snapshots_evaluated": evaluated,
        "cache_hits": cache_hits,
        "eval_errors": eval_errors,
        "markets": len(outcomes),
        "bankroll_usdc": str(_BANKROLL),
        "profiles": [conservative.summary(), aggressive.summary()],
        # Aggregate + verdict here; full per-snapshot rows live in the
        # enrichment artifact to keep this file lean.
        "enrichment_diagnostic": diagnostic.model_dump(
            mode="json", exclude={"records"}
        ),
    }

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    enrichment_path = Path(args.enrichment_output)
    enrichment_path.parent.mkdir(parents=True, exist_ok=True)
    enrichment_path.write_text(diagnostic.model_dump_json(indent=2), encoding="utf-8")

    print("\n=== Risk-Profile Comparison Backtest ===")
    print(
        f"Provider: {config.llm_provider} | snapshots: {evaluated} | errors: {eval_errors}"
    )
    for s in report["profiles"]:
        print(
            f"[{s['label']:>12} c>={s['min_confidence']} ev>={s['min_ev_threshold']}] "
            f"trades={s['accepted_trades']:>3} resolved={s['resolved_trades']:>3} "
            f"win_rate={s['win_rate']} net_pnl={s['net_pnl_usdc']:>10} "
            f"avg_conf={s['avg_confidence']} avg_ev={s['avg_ev']}"
        )
    print("\n=== WI-68 Enrichment Diagnostic (|p_true - midpoint|) ===")
    print(
        f"records={diagnostic.record_count} enriched={diagnostic.enriched_count} "
        f"skips={diagnostic.enriched_skips} "
        f"materiality>={diagnostic.materiality_threshold}"
    )
    print(
        f"baseline delta  mean={diagnostic.baseline_delta_mean} "
        f"median={diagnostic.baseline_delta_median} max={diagnostic.baseline_delta_max}"
    )
    print(
        f"enriched delta  mean={diagnostic.enriched_delta_mean} "
        f"median={diagnostic.enriched_delta_median} max={diagnostic.enriched_delta_max} "
        f"above_threshold={diagnostic.enriched_above_threshold}"
    )
    print(f"VERDICT: {diagnostic.verdict.value} — {diagnostic.verdict_summary}")
    print(f"\nReport: {out_path}\nEnrichment: {enrichment_path}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
