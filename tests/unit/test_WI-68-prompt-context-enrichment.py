"""WI-68 — Prompt Context Enrichment + Re-diagnosis.

Prompt enrichment (``src/agents/context/prompt_factory.py``): build_evaluation_prompt
must render the market question already carried in ``market_state`` (production carries
it; the template previously dropped it), with a NEUTRAL fallback when absent — never
fabricated text. Sentiment handling unchanged. Lookahead-safe.

Backtest re-diagnosis (``scripts/run_profile_comparison_backtest.py``): baseline (no
question) vs enriched (question) arms; per-snapshot |p_true - midpoint| Decimal delta
record + aggregate report; Gamma question fetch with explicit timeout + bounded retry,
failing closed to a typed skip; cache key separates the two arms.
"""

import json
from decimal import Decimal

import httpx
import pytest
from pydantic import ValidationError

from src.agents.context.prompt_factory import PromptFactory, _QUESTION_UNAVAILABLE
from src.schemas.llm import MarketCategory, SentimentResponse

from scripts.run_profile_comparison_backtest import (
    EnrichmentArmStatus,
    EnrichmentDeltaRecord,
    EnrichmentDiagnosticReport,
    EnrichmentVerdict,
    QuestionFetchResult,
    _cache_key,
    _fetch_question,
    _p_true,
    _realized_pnl,
)

_QUESTION = "Will BTC close above $100,000 on 2026-12-31?"
_SENTINEL_OUTCOME = "RESOLVED_OUTCOME_SENTINEL_XYZ"

_BASE_STATE = {
    "condition_id": "0xabc",
    "best_bid": 0.40,
    "best_ask": 0.42,
    "midpoint": 0.41,
    "spread": 0.02,
    "timestamp": "2026-05-01T00:00:00+00:00",
}


def _state(**overrides):
    s = dict(_BASE_STATE)
    s.update(overrides)
    return s


# ---------------------------------------------------------------------------
# Fakes for the async Gamma fetch
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _FakeHTTP:
    def __init__(self, *, exc=None, status_code=200, payload=None):
        self.calls = []
        self._exc = exc
        self._status_code = status_code
        self._payload = payload or {}

    async def get(self, url, timeout=None):
        self.calls.append({"url": url, "timeout": timeout})
        if self._exc is not None:
            raise self._exc
        return _FakeResp(self._status_code, self._payload)


# ---------------------------------------------------------------------------
# Prompt enrichment — PromptFactory.build_evaluation_prompt
# ---------------------------------------------------------------------------


def test_question_rendered_when_present():
    prompt = PromptFactory.build_evaluation_prompt(_state(question=_QUESTION))
    assert _QUESTION in prompt
    assert _QUESTION_UNAVAILABLE not in prompt


def test_neutral_fallback_when_question_missing():
    prompt = PromptFactory.build_evaluation_prompt(_state())  # no question key
    assert _QUESTION_UNAVAILABLE in prompt


def test_neutral_fallback_when_question_empty_string():
    prompt = PromptFactory.build_evaluation_prompt(_state(question="   "))
    assert _QUESTION_UNAVAILABLE in prompt


def test_fallback_text_is_not_fabricated():
    # The fallback marker is a fixed neutral string — no invented question,
    # number, odds, or balance.
    assert "unavailable" in _QUESTION_UNAVAILABLE.lower()
    assert not any(ch.isdigit() for ch in _QUESTION_UNAVAILABLE)
    present = PromptFactory.build_evaluation_prompt(_state(question=_QUESTION))
    absent = PromptFactory.build_evaluation_prompt(_state())
    assert present != absent


def test_sentiment_block_unchanged_with_sentiment():
    sentiment = SentimentResponse(
        sentiment_score=Decimal("0.5"),
        tweet_volume_delta=12,
        top_narrative_summary="Strong bullish momentum across the board today.",
    )
    block = PromptFactory._build_sentiment_block(sentiment)
    prompt = PromptFactory.build_evaluation_prompt(
        _state(question=_QUESTION), sentiment=sentiment
    )
    assert block in prompt


def test_sentiment_block_unchanged_when_none():
    block = PromptFactory._build_sentiment_block(None)
    prompt = PromptFactory.build_evaluation_prompt(_state(question=_QUESTION))
    assert block in prompt
    assert "neutral — no oracle data available" in prompt


def test_category_persona_still_selected():
    prompt = PromptFactory.build_evaluation_prompt(
        _state(question=_QUESTION), category=MarketCategory.CRYPTO
    )
    assert "on-chain analyst" in prompt
    assert _QUESTION in prompt


def test_resolved_outcome_never_in_prompt():
    # Even if a resolved-outcome value leaks into the state dict, the prompt
    # builder must never surface it (lookahead safety).
    prompt = PromptFactory.build_evaluation_prompt(
        _state(question=_QUESTION, resolved_outcome=_SENTINEL_OUTCOME)
    )
    assert _SENTINEL_OUTCOME not in prompt
    assert "resolved_outcome" not in prompt


# ---------------------------------------------------------------------------
# Backtest diagnostic — typed, Decimal-native models
# ---------------------------------------------------------------------------


def test_enrichment_delta_record_is_decimal_native():
    with pytest.raises(ValidationError):
        EnrichmentDeltaRecord(
            token_id="t",
            condition_id="c",
            midpoint=0.41,  # raw float — must be rejected
            p_true_baseline=Decimal("0.41"),
            delta_baseline=Decimal("0.0"),
            question_present=False,
        )


def test_enrichment_delta_record_abs_delta_math():
    rec = EnrichmentDeltaRecord.build(
        token_id="t",
        condition_id="c",
        midpoint=Decimal("0.40"),
        p_true_baseline=Decimal("0.55"),
        p_true_enriched=Decimal("0.30"),
    )
    assert rec.delta_baseline == Decimal("0.15")
    assert rec.delta_enriched == Decimal("0.10")
    assert rec.question_present is True

    skipped = EnrichmentDeltaRecord.build(
        token_id="t",
        condition_id="c",
        midpoint=Decimal("0.40"),
        p_true_baseline=Decimal("0.55"),
        p_true_enriched=None,
    )
    assert skipped.delta_enriched is None
    assert skipped.question_present is False


def _records():
    return [
        EnrichmentDeltaRecord.build(
            token_id="t",
            condition_id="c",
            midpoint=Decimal("0.50"),
            p_true_baseline=Decimal("0.51"),
            p_true_enriched=Decimal("0.51"),  # delta 0.01
        ),
        EnrichmentDeltaRecord.build(
            token_id="t",
            condition_id="c",
            midpoint=Decimal("0.50"),
            p_true_baseline=Decimal("0.52"),
            p_true_enriched=Decimal("0.60"),  # delta 0.10
        ),
        EnrichmentDeltaRecord.build(
            token_id="t",
            condition_id="c",
            midpoint=Decimal("0.50"),
            p_true_baseline=Decimal("0.53"),
            p_true_enriched=Decimal("0.70"),  # delta 0.20
        ),
    ]


def test_diagnostic_report_aggregate_stats():
    report = EnrichmentDiagnosticReport.from_records(
        _records(), materiality_threshold=Decimal("0.05")
    )
    assert report.record_count == 3
    assert report.enriched_count == 3
    # enriched deltas: 0.01, 0.10, 0.20
    assert report.enriched_delta_max == Decimal("0.20")
    assert report.enriched_delta_median == Decimal("0.10")
    assert report.enriched_delta_mean == (
        (Decimal("0.01") + Decimal("0.10") + Decimal("0.20")) / Decimal("3")
    )


def test_diagnostic_report_counts_above_materiality_threshold():
    report = EnrichmentDiagnosticReport.from_records(
        _records(), materiality_threshold=Decimal("0.05")
    )
    # 0.10 and 0.20 exceed 0.05; 0.01 does not
    assert report.enriched_above_threshold == 2
    assert report.enriched_skips == 0


def test_report_serializes_decimals_as_strings():
    report = EnrichmentDiagnosticReport.from_records(
        _records(), materiality_threshold=Decimal("0.05")
    )
    obj = json.loads(report.model_dump_json())
    assert isinstance(obj["materiality_threshold"], str)
    assert isinstance(obj["enriched_delta_mean"], str)
    assert isinstance(obj["baseline_delta_max"], str)


def test_report_persists_per_snapshot_records():
    # WI-68 DoD: per-snapshot deltas persisted alongside the aggregate.
    report = EnrichmentDiagnosticReport.from_records(
        _records(), materiality_threshold=Decimal("0.05")
    )
    assert len(report.records) == 3
    assert all(isinstance(r, EnrichmentDeltaRecord) for r in report.records)
    obj = json.loads(report.model_dump_json())
    assert isinstance(obj["records"], list)
    assert len(obj["records"]) == 3
    assert isinstance(obj["records"][0]["delta_baseline"], str)
    assert obj["records"][1]["delta_enriched"] == "0.10"


def test_verdict_prompt_starvation():
    # Enriched deltas (0.01/0.10/0.20, mean ~0.103) exceed materiality and the
    # baseline arm → alpha discarded at the prompt layer (verdict a).
    report = EnrichmentDiagnosticReport.from_records(
        _records(), materiality_threshold=Decimal("0.05")
    )
    assert report.verdict is EnrichmentVerdict.PROMPT_STARVATION
    assert report.verdict_summary.startswith("(a)")


def test_verdict_llm_weak():
    # Enriched deltas stay tiny and near baseline → LLM-alone weak (verdict b).
    recs = [
        EnrichmentDeltaRecord.build(
            token_id="t",
            condition_id="c",
            midpoint=Decimal("0.50"),
            p_true_baseline=Decimal("0.51"),
            p_true_enriched=Decimal("0.515"),
        ),
        EnrichmentDeltaRecord.build(
            token_id="t",
            condition_id="c",
            midpoint=Decimal("0.50"),
            p_true_baseline=Decimal("0.49"),
            p_true_enriched=Decimal("0.492"),
        ),
    ]
    report = EnrichmentDiagnosticReport.from_records(
        recs, materiality_threshold=Decimal("0.05")
    )
    assert report.verdict is EnrichmentVerdict.LLM_WEAK
    assert report.verdict_summary.startswith("(b)")


def test_verdict_insufficient_data():
    # No enriched arm produced (question never sourced) → unresolved confound.
    recs = [
        EnrichmentDeltaRecord.build(
            token_id="t",
            condition_id="c",
            midpoint=Decimal("0.50"),
            p_true_baseline=Decimal("0.51"),
            p_true_enriched=None,
        ),
    ]
    report = EnrichmentDiagnosticReport.from_records(
        recs, materiality_threshold=Decimal("0.05")
    )
    assert report.verdict is EnrichmentVerdict.INSUFFICIENT_DATA
    assert report.enriched_skips == 1


# ---------------------------------------------------------------------------
# Backtest harness — arms, cache key, Gamma fetch
# ---------------------------------------------------------------------------


def test_baseline_market_state_has_no_question():
    # Baseline arm omits the question → reproduces the WI-67 starved prompt.
    baseline_state = _state()  # no question
    prompt = PromptFactory.build_evaluation_prompt(baseline_state)
    assert _QUESTION_UNAVAILABLE in prompt


def test_enriched_market_state_carries_question():
    enriched_state = _state(question=_QUESTION)
    prompt = PromptFactory.build_evaluation_prompt(enriched_state)
    assert _QUESTION in prompt


def test_cache_key_separates_baseline_and_enriched():
    base = _cache_key("tok", "2026-05-01", "baseline")
    enr = _cache_key("tok", "2026-05-01", "enriched")
    assert base != enr
    assert "baseline" in base
    assert "enriched" in enr


@pytest.mark.asyncio
async def test_gamma_question_fetch_has_timeout_and_bounded_retry():
    http = _FakeHTTP(exc=httpx.TimeoutException("boom"))
    result = await _fetch_question(
        "0xabc", http, "https://gamma.example/api", retries=2
    )
    assert result.status is EnrichmentArmStatus.SKIPPED_NO_QUESTION
    # retries=2 → at most 3 attempts; bounded, not unbounded
    assert len(http.calls) == 3
    # explicit timeout passed on every call
    assert all(call["timeout"] is not None for call in http.calls)


@pytest.mark.asyncio
async def test_gamma_fetch_failure_skips_enriched_arm_typed():
    http = _FakeHTTP(exc=httpx.ConnectError("down"))
    result = await _fetch_question("0xabc", http, "https://gamma.example/api")
    assert isinstance(result, QuestionFetchResult)
    assert result.status is EnrichmentArmStatus.SKIPPED_NO_QUESTION
    assert result.question is None


@pytest.mark.asyncio
async def test_gamma_fetch_success_returns_question_typed():
    http = _FakeHTTP(status_code=200, payload={"question": _QUESTION})
    result = await _fetch_question("0xabc", http, "https://gamma.example/api")
    assert result.status is EnrichmentArmStatus.ENRICHED
    assert result.question == _QUESTION


def test_outcome_read_only_for_pnl_not_prompt():
    # The resolved outcome drives PnL...
    win = _realized_pnl(
        position_size_pct=Decimal("0.03"),
        entry_midpoint=Decimal("0.40"),
        outcome="YES",
    )
    lose = _realized_pnl(
        position_size_pct=Decimal("0.03"),
        entry_midpoint=Decimal("0.40"),
        outcome="NO",
    )
    assert win != lose
    # ...but never reaches the prompt.
    prompt = PromptFactory.build_evaluation_prompt(
        _state(question=_QUESTION, resolved_outcome="YES")
    )
    assert "resolved_outcome" not in prompt


def test_realized_pnl_decimal_and_midpoint_guard():
    for bad_mid in (Decimal("0"), Decimal("1"), Decimal("1.5")):
        assert _realized_pnl(
            position_size_pct=Decimal("0.03"),
            entry_midpoint=bad_mid,
            outcome="YES",
        ) == Decimal("0")
    win = _realized_pnl(
        position_size_pct=Decimal("0.03"),
        entry_midpoint=Decimal("0.40"),
        outcome="YES",
    )
    assert isinstance(win, Decimal)
    assert win > Decimal("0")
    loss = _realized_pnl(
        position_size_pct=Decimal("0.03"),
        entry_midpoint=Decimal("0.40"),
        outcome="NO",
    )
    assert loss < Decimal("0")


def test_p_true_extracts_decimal():
    assert _p_true({"probabilistic_estimate": {"p_true": "0.62"}}) == Decimal("0.62")
    assert _p_true({"probabilistic_estimate": {}}) is None
    assert _p_true(None) is None
    assert _p_true({}) is None
