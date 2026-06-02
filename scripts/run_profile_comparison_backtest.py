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
from pathlib import Path
from typing import Any

import structlog

from src.agents.context.prompt_factory import PromptFactory
from src.agents.evaluation.claude_client import ClaudeClient
from src.core.config import get_config
from src.schemas.llm import LLMEvaluationResponse, RecommendedAction

logger = structlog.get_logger(__name__)

_BANKROLL = Decimal("1000")


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
    logger.info(
        "profile_backtest.start",
        snapshots=len(snapshots),
        markets=len(outcomes),
        provider=config.llm_provider,
        cached=len(cache),
    )

    for idx, snap in enumerate(snapshots):
        token = str(snap.get("token_id", ""))
        outcome = outcomes.get(token)
        key = f"{token}|{snap.get('timestamp_utc', '')}"

        parsed = cache.get(key)
        if parsed is not None:
            cache_hits += 1
        else:
            market_state = {
                "condition_id": snap.get("condition_id", "Unknown"),
                "best_bid": float(snap["best_bid"]),
                "best_ask": float(snap["best_ask"]),
                "midpoint": float(snap["midpoint"]),
                "spread": float(snap.get("spread", 0.0)),
                "timestamp": snap.get("timestamp_utc", ""),
            }
            prompt = PromptFactory.build_evaluation_prompt(market_state)
            try:
                parsed, _usage, _block = await client.evaluate_for_backtest(
                    prompt,
                    snapshot_id=f"bt-{idx}",
                    market_key=f"bt-{token}-{idx}",  # unique → no cooldown suppression
                )
            except Exception as exc:
                eval_errors += 1
                logger.warning("profile_backtest.eval_failed", idx=idx, error=str(exc))
                continue
            cache[key] = parsed
            cache_fh.write(
                json.dumps({"key": key, "parsed": parsed}, default=str) + "\n"
            )
            cache_fh.flush()

        evaluated += 1
        conservative.apply(parsed, snap, outcome)
        aggressive.apply(parsed, snap, outcome)

        if (idx + 1) % 25 == 0:
            logger.info(
                "profile_backtest.progress",
                done=idx + 1,
                total=len(snapshots),
                cons_trades=conservative.trades,
                aggr_trades=aggressive.trades,
            )

    cache_fh.close()

    report = {
        "data_dir": str(data_dir),
        "snapshots_evaluated": evaluated,
        "cache_hits": cache_hits,
        "eval_errors": eval_errors,
        "markets": len(outcomes),
        "bankroll_usdc": str(_BANKROLL),
        "profiles": [conservative.summary(), aggressive.summary()],
    }

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

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
    print(f"\nReport: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
