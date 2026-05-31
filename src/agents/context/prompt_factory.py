"""
src/agents/context/prompt_factory.py

Constructs prompts for the LLM Evaluation Node, injecting live market data
into a strict Chain-of-Thought architecture.
"""

from __future__ import annotations

from typing import Dict, Any

from src.schemas.llm import (
    LLMEvaluationResponse,
    MarketCategory,
    ReflectionResponse,
    SentimentResponse,
    KELLY_FRACTION,
    MIN_CONFIDENCE,
    MAX_SPREAD_PCT,
    MAX_EXPOSURE_PCT,
    MIN_EV_THRESHOLD,
    MIN_TTR_HOURS,
)

_PERSONA_MAP: Dict[MarketCategory, str] = {
    MarketCategory.POLITICS: (
        "You are a political risk analyst with expertise in electoral forecasting, "
        "geopolitical event modelling, and prediction market calibration."
    ),
    MarketCategory.SPORTS: (
        "You are a quantitative sports analyst specialising in statistical modelling, "
        "real-time line movement analysis, and injury-impact assessment."
    ),
    MarketCategory.CRYPTO: (
        "You are a senior on-chain analyst and crypto derivatives trader with deep expertise "
        "in blockchain fundamentals, tokenomics, and macro crypto market cycles."
    ),
    MarketCategory.ESPORTS: (
        "You are a quantitative esports markets analyst with expertise in team form, "
        "patch dynamics, roster changes, and tournament incentives."
    ),
    MarketCategory.IRAN: (
        "You are a geopolitical risk analyst specializing in Iran-related policy, "
        "regional security, sanctions, and diplomatic event forecasting."
    ),
    MarketCategory.FINANCE: (
        "You are a macro and financial markets analyst with expertise in rates, "
        "equities, commodities, credit, and prediction-market pricing."
    ),
    MarketCategory.GEOPOLITICS: (
        "You are a geopolitical forecasting analyst with expertise in state behavior, "
        "conflict risk, diplomacy, and international institutions."
    ),
    MarketCategory.TECH: (
        "You are a technology sector analyst with expertise in product launches, "
        "platform policy, AI, semiconductors, and adoption curves."
    ),
    MarketCategory.CULTURE: (
        "You are a culture and media markets analyst with expertise in public attention, "
        "entertainment dynamics, and prediction-market calibration."
    ),
    MarketCategory.ECONOMY: (
        "You are an economic data analyst with expertise in inflation, labor markets, "
        "growth indicators, central banks, and macro forecasting."
    ),
    MarketCategory.WEATHER: (
        "You are a weather and climate risk analyst with expertise in forecast uncertainty, "
        "event thresholds, and regional meteorological data."
    ),
    MarketCategory.MENTIONS: (
        "You are a public-attention analyst with expertise in media monitoring, "
        "mention-volume dynamics, and event-driven information markets."
    ),
    MarketCategory.ELECTIONS: (
        "You are an elections forecaster with expertise in polling, turnout, "
        "district fundamentals, and prediction-market calibration."
    ),
}

_DEFAULT_PERSONA = (
    "You are an elite Staff Quantitative Developer at a top proprietary trading firm."
)


class PromptFactory:
    """
    Builds the explicit instructions and injects current state for the LLM.
    Ensures the LLM understands its role as a Quant Developer and enforces
    strict JSON output matching the Pydantic schema.
    """

    @staticmethod
    def _build_sentiment_block(sentiment: SentimentResponse | None) -> str:
        """Build the sentiment oracle section for prompt injection."""
        if sentiment is None:
            return (
                "### SENTIMENT ORACLE (LAST 60 MIN)\n"
                "Sentiment Score: 0.0 (neutral — no oracle data available)\n"
                "Tweet Volume Delta: 0%\n"
                "Narrative: No sentiment signal; evaluate on market fundamentals only.\n"
            )
        return (
            "### SENTIMENT ORACLE (LAST 60 MIN)\n"
            f"Sentiment Score: {sentiment.sentiment_score}\n"
            f"Tweet Volume Delta: {sentiment.tweet_volume_delta}%\n"
            f"Narrative: {sentiment.top_narrative_summary}\n"
        )

    @staticmethod
    def build_evaluation_prompt(
        market_state: Dict[str, Any],
        category: MarketCategory | None = None,
        sentiment: SentimentResponse | None = None,
    ) -> str:
        """
        Constructs the Chain-of-Thought evaluation prompt.

        Args:
            market_state: Dictionary containing condition_id, best_bid, best_ask,
                          midpoint, spread, and timestamp.
            category: Market domain category for persona selection.
            sentiment: Validated Stage A sentiment artifact, or None for neutral fallback.

        Returns:
            The complete formatted prompt string to send to the LLM.
        """
        json_schema = LLMEvaluationResponse.model_json_schema()
        persona = _PERSONA_MAP.get(category, _DEFAULT_PERSONA)
        sentiment_block = PromptFactory._build_sentiment_block(sentiment)

        prompt = f"""{persona}
Your objective is to estimate the true probability of a live binary options market on Polymarket resolving to 'YES', and to judge the quality and risk of that estimate. The system computes Expected Value, spread, Kelly sizing, and the final trade decision deterministically from your estimate and authoritative market data — you must not perform those calculations.

### LIVE MARKET DATA SNAPSHOT
Condition ID: {market_state.get("condition_id", "Unknown")}
Best Bid: {market_state.get("best_bid", 0.0):.4f} USDC
Best Ask: {market_state.get("best_ask", 0.0):.4f} USDC
Midpoint (Implied Market Probability): {market_state.get("midpoint", 0.0):.4f}
Bid-Ask Spread: {market_state.get("spread", 0.0):.4f} USDC
Timestamp: {market_state.get("timestamp", 0.0)}

{sentiment_block}
### INSTRUCTIONS
1. Analyze the given market parameters and the sentiment oracle signal above.
2. Estimate the True Probability (p_true) of the underlying event resolving to 'YES', grounded in cited evidence and your domain knowledge.
3. State your epistemic confidence in that estimate and explain your reasoning, citing the evidence behind p_true.
4. Provide a qualitative risk assessment (liquidity, resolution, information asymmetry).
5. The system computes Expected Value, spread, Kelly sizing, and position size deterministically from authoritative market data — do not calculate or assert any of these numbers yourself.

### CRITICAL OUTPUT FORMAT
You MUST reply ONLY with a raw, valid JSON object that strictly adheres to the following JSON schema.
Do NOT wrap the JSON in markdown blocks (e.g., ```json ... ```) or add any conversational text before or after the JSON.
Any deviation from this format will cause the system pipeline to crash.

JSON Schema for your output:
{json_schema}
"""
        return prompt

    @staticmethod
    def build_reflection_prompt(
        *,
        market_state: Dict[str, Any],
        sentiment: SentimentResponse | None,
        primary_candidate_json: str,
        snapshot_id: str,
    ) -> str:
        """Build the adversarial reflection auditor prompt (Stage C).

        The reflection LLM acts as a ruthless quantitative risk auditor
        that challenges the primary evaluation for bias, data inconsistency,
        and risk drift.
        """
        sentiment_block = PromptFactory._build_sentiment_block(sentiment)
        reflection_schema = ReflectionResponse.model_json_schema()

        return f"""You are an adversarial quantitative risk auditor. Your job is to challenge the primary evaluation for bias, data inconsistency, and risk drift. Prefer conservative outcomes under unresolved uncertainty. Return strict JSON only.

### SNAPSHOT
snapshot_id: {snapshot_id}

### MARKET STATE
Condition ID: {market_state.get("condition_id", "Unknown")}
Best Bid: {market_state.get("best_bid", 0.0)}
Best Ask: {market_state.get("best_ask", 0.0)}
Midpoint: {market_state.get("midpoint", 0.0)}
Spread: {market_state.get("spread", 0.0)}
Timestamp: {market_state.get("timestamp", 0)}

{sentiment_block}
### RISK CONSTANTS
KELLY_FRACTION={KELLY_FRACTION}
MIN_CONFIDENCE={MIN_CONFIDENCE}
MAX_SPREAD_PCT={MAX_SPREAD_PCT}
MAX_EXPOSURE_PCT={MAX_EXPOSURE_PCT}
MIN_EV_THRESHOLD={MIN_EV_THRESHOLD}
MIN_TTR_HOURS={MIN_TTR_HOURS}

### PRIMARY CANDIDATE (Stage B output)
{primary_candidate_json}

### SYSTEM-COMPUTED VALUES (DO NOT RE-AUDIT)
Expected Value, net odds, Kelly sizing, spread percentage, and position size are computed deterministically by the system from authoritative market data. Do not recompute, second-guess, or flag these values or their arithmetic. Audit only the quality of the primary candidate's judgment (its p_true estimate and the reasoning behind it).

### AUDIT QUESTIONS (answer each explicitly)
1. Evidence check: Is p_true supported by cited, relevant evidence, or is it an unsupported assertion?
2. Bias check: Does the reasoning show confirmation bias, recency bias, narrative anchoring, or overconfidence unsupported by evidence?
3. Uncertainty check: If assumptions are unsupported or contradictory, should the decision default to HOLD?
4. Decision coherence check: Are decision_boolean and recommended_action logically consistent with the stated p_true and confidence?

### OUTPUT
Return ONLY a raw JSON object matching this schema (no markdown, no commentary):
{reflection_schema}

Verdict rules:
- APPROVED: candidate passes all checks unchanged.
- ADJUSTED: provide correction_instructions and corrected_candidate_json with fixes.
- REJECTED: candidate has fatal bias or inconsistency; force HOLD.
"""
