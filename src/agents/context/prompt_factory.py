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
    MarketCategory.CRYPTO: (
        "You are a senior on-chain analyst and crypto derivatives trader with deep expertise "
        "in blockchain fundamentals, tokenomics, and macro crypto market cycles."
    ),
    MarketCategory.POLITICS: (
        "You are a political risk analyst with expertise in electoral forecasting, "
        "geopolitical event modelling, and prediction market calibration."
    ),
    MarketCategory.SPORTS: (
        "You are a quantitative sports analyst specialising in statistical modelling, "
        "real-time line movement analysis, and injury-impact assessment."
    ),
    MarketCategory.GENERAL: (
        "You are an elite Staff Quantitative Developer at a top proprietary trading firm."
    ),
}

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
        category: MarketCategory = MarketCategory.GENERAL,
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
        persona = _PERSONA_MAP[category]
        sentiment_block = PromptFactory._build_sentiment_block(sentiment)

        prompt = f"""{persona}
Your objective is to evaluate a live binary options market on Polymarket and determine if there is a positive Expected Value (EV) trading opportunity.

### LIVE MARKET DATA SNAPSHOT
Condition ID: {market_state.get('condition_id', 'Unknown')}
Best Bid: {market_state.get('best_bid', 0.0):.4f} USDC
Best Ask: {market_state.get('best_ask', 0.0):.4f} USDC
Midpoint (Implied Market Probability): {market_state.get('midpoint', 0.0):.4f}
Bid-Ask Spread: {market_state.get('spread', 0.0):.4f} USDC
Timestamp: {market_state.get('timestamp', 0.0)}

{sentiment_block}
### INSTRUCTIONS
1. Analyze the given market parameters and the sentiment oracle signal above.
2. Estimate the True Probability of the underlying event resolving to 'YES'. Use your internal knowledge or provided context to establish this.
3. Calculate the Expected Value (EV). Recall: EV = (True Probability * Profit) - ((1 - True Probability) * Loss).
4. Apply the required safety filters (e.g., EV > 2%, Spread < 1.5%, Confidence >= 75%).
5. Output your reasoning and final decision.

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
Condition ID: {market_state.get('condition_id', 'Unknown')}
Best Bid: {market_state.get('best_bid', 0.0)}
Best Ask: {market_state.get('best_ask', 0.0)}
Midpoint: {market_state.get('midpoint', 0.0)}
Spread: {market_state.get('spread', 0.0)}
Timestamp: {market_state.get('timestamp', 0)}

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

### AUDIT QUESTIONS (answer each explicitly)
1. Bias check: Does reasoning show confirmation bias, recency bias, narrative anchoring, or overconfidence unsupported by evidence?
2. Data consistency check: Are bid/ask/midpoint/spread relationships coherent with market snapshot values?
3. Probability/EV consistency: Are p_true, p_market, and EV arithmetic internally consistent?
4. Risk sanity check: Does proposed sizing align with quarter-Kelly and 3% cap policy?
5. Gatekeeper pre-check: Would any mandatory safety filter clearly fail (EV threshold, confidence, spread, TTR)?
6. Decision coherence check: Are decision_boolean, recommended_action, and size logically consistent?
7. Uncertainty check: If assumptions are unsupported or contradictory, should decision default to HOLD?

### OUTPUT
Return ONLY a raw JSON object matching this schema (no markdown, no commentary):
{reflection_schema}

Verdict rules:
- APPROVED: candidate passes all checks unchanged.
- ADJUSTED: provide correction_instructions and corrected_candidate_json with fixes.
- REJECTED: candidate has fatal bias or inconsistency; force HOLD.
"""
