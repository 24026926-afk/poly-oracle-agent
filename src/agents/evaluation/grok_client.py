"""
src/agents/evaluation/grok_client.py

Async Grok Sentiment Oracle client for WI-12 chained evaluation.

Mock-first design: returns deterministic fixtures in mock mode.
Production-ready httpx interface for live Grok API calls when mocked=False.

Invariants:
  - Strict 2.0s timeout per request (asyncio.wait_for + httpx timeout).
  - Any failure returns neutral SentimentResponse — never stalls the pipeline.
  - Decimal-only for sentiment_score — no float math.
"""

from __future__ import annotations

import asyncio
import json
import re
from decimal import Decimal

import httpx
import structlog
from pydantic import SecretStr, ValidationError

from src.schemas.llm import MarketCategory, SentimentResponse

logger = structlog.get_logger(__name__)

# Neutral fallback — used on timeout, HTTP error, or schema validation failure
NEUTRAL_SENTIMENT = SentimentResponse(
    sentiment_score=Decimal("0.0"),
    tweet_volume_delta=0,
    top_narrative_summary="Sentiment unavailable in 2.0s window; neutral fallback applied.",
)

# Deterministic mock fixture for tests and local runs
_MOCK_SENTIMENT = SentimentResponse(
    sentiment_score=Decimal("0.65"),
    tweet_volume_delta=12,
    top_narrative_summary="Moderate positive sentiment detected across social channels in the last 60 minutes.",
)

GROK_TIMEOUT_SECONDS: float = 2.0

_SYSTEM_PROMPT = (
    "You are a market sentiment extraction engine. Return only strict JSON."
)

_USER_PROMPT_TEMPLATE = """\
Analyze public X/Twitter discourse for this market in the last 60 minutes.

Market details:
- condition_id: {condition_id}
- market_title: {market_title}
- market_category: {market_category}
- reference_timestamp_utc: {reference_timestamp_utc}
- analysis_window_minutes: 60
{extra_context}

Instructions:
1. Estimate directional sentiment and participation momentum.
2. Return exactly one JSON object with these keys:
   - "sentiment_score": float in [-1.0, 1.0] (-1 = bearish, +1 = bullish)
   - "tweet_volume_delta": signed integer percent delta vs prior 60-min baseline
   - "top_narrative_summary": 1-2 sentence summary of dominant narrative (10-320 chars)
"""


class GrokClient:
    """Async Grok Sentiment Oracle — mock-first with production-ready httpx signatures."""

    def __init__(
        self,
        api_key: SecretStr,
        base_url: str,
        model: str,
        http_client: httpx.AsyncClient | None = None,
        timeout_seconds: float = GROK_TIMEOUT_SECONDS,
        mocked: bool = True,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._http_client = http_client
        self._timeout = timeout_seconds
        self._mocked = mocked

    async def analyze_sentiment(
        self,
        *,
        condition_id: str,
        market_title: str,
        market_category: MarketCategory,
        reference_timestamp_utc: str,
        tags: list[str] | None = None,
    ) -> SentimentResponse:
        """Fetch sentiment from Grok (or return mock fixture).

        On any failure (timeout, HTTP error, malformed JSON, schema error),
        returns NEUTRAL_SENTIMENT and logs the reason. Never raises.
        """
        if self._mocked:
            logger.info(
                "grok_sentiment_mock",
                condition_id=condition_id,
                category=market_category.value,
            )
            return _MOCK_SENTIMENT

        return await self._fetch_live(
            condition_id=condition_id,
            market_title=market_title,
            market_category=market_category,
            reference_timestamp_utc=reference_timestamp_utc,
            tags=tags,
        )

    async def _fetch_live(
        self,
        *,
        condition_id: str,
        market_title: str,
        market_category: MarketCategory,
        reference_timestamp_utc: str,
        tags: list[str] | None,
    ) -> SentimentResponse:
        """Live Grok API call with strict timeout and fallback."""
        extra_lines = ""
        if tags:
            extra_lines += f"- tags: {', '.join(tags)}\n"

        user_content = _USER_PROMPT_TEMPLATE.format(
            condition_id=condition_id,
            market_title=market_title,
            market_category=market_category.value,
            reference_timestamp_utc=reference_timestamp_utc,
            extra_context=extra_lines,
        )

        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.0,
        }

        headers = {
            "Authorization": f"Bearer {self._api_key.get_secret_value()}",
            "Content-Type": "application/json",
        }

        try:
            client = self._http_client or httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout),
            )
            try:
                resp = await asyncio.wait_for(
                    client.post(
                        f"{self._base_url}/chat/completions",
                        json=payload,
                        headers=headers,
                    ),
                    timeout=self._timeout,
                )
                resp.raise_for_status()
                body = resp.json()
                raw_text = body["choices"][0]["message"]["content"]
                json_str = self._extract_json(raw_text)
                return SentimentResponse.model_validate_json(json_str)

            finally:
                if self._http_client is None:
                    await client.aclose()

        except asyncio.TimeoutError:
            logger.warning(
                "grok_sentiment_timeout",
                condition_id=condition_id,
                timeout_seconds=self._timeout,
            )
            return NEUTRAL_SENTIMENT

        except httpx.HTTPStatusError as exc:
            logger.warning(
                "grok_sentiment_http_error",
                condition_id=condition_id,
                status_code=exc.response.status_code,
            )
            return NEUTRAL_SENTIMENT

        except (ValidationError, KeyError, json.JSONDecodeError) as exc:
            logger.warning(
                "grok_sentiment_schema_error",
                condition_id=condition_id,
                error=str(exc),
            )
            return NEUTRAL_SENTIMENT

        except Exception as exc:
            logger.warning(
                "grok_sentiment_unexpected_error",
                condition_id=condition_id,
                error=str(exc),
            )
            return NEUTRAL_SENTIMENT

    @staticmethod
    def _extract_json(text: str) -> str:
        """Extract JSON object from potential markdown wrapper."""
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if match:
            return match.group(1).strip()
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            return text[start : end + 1].strip()
        return text.strip()
