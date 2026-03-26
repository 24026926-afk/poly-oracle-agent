"""
src/agents/context/prompt_factory.py

Constructs prompts for the LLM Evaluation Node, injecting live market data
into a strict Chain-of-Thought architecture.
"""

from __future__ import annotations

from typing import Dict, Any

from src.schemas.llm import LLMEvaluationResponse, MarketCategory, SentimentResponse

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
