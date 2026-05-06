"""
src/agents/evaluation/grok_client.py

Async Grok Sentiment Oracle client for WI-12 chained evaluation.

Mock-first design: returns deterministic fixtures in mock mode.
Production-ready httpx interface for live Grok API calls when mocked=False.

Invariants:
  - Strict timeout per request (asyncio.wait_for + httpx timeout).
  - Total budget enforcement: retries cannot exceed total_budget_seconds.
  - Any failure returns neutral SentimentResponse — never stalls the pipeline.
  - Decimal-only for sentiment_score — JSON floats parsed as Decimal at boundary.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from decimal import Decimal
from typing import Any

import httpx
import structlog
from pydantic import SecretStr, ValidationError

from src.schemas.llm import (
    GrokFailureReason,
    GrokLiveConfig,
    GrokRequestEnvelope,
    GrokResponseEnvelope,
    MarketCategory,
    SentimentResponse,
)

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
GROK_TOTAL_BUDGET_SECONDS: float = 4.0  # conservative default for 2 retries × 2s

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
    """Async Grok Sentiment Oracle — mock-first with production-ready live xAI/Grok path.

    Exposes ``last_failure_reason`` for audit trail propagation.
    """

    def __init__(
        self,
        api_key: SecretStr,
        base_url: str,
        model: str,
        http_client: httpx.AsyncClient | None = None,
        timeout_seconds: float = GROK_TIMEOUT_SECONDS,
        mocked: bool = True,
        live_enabled: bool = False,
        max_retries: int = 2,
        total_budget_seconds: float | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._http_client = http_client
        self._mocked = mocked

        # Validate live config through typed model
        live_cfg = GrokLiveConfig(
            live_enabled=live_enabled,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
        )
        self._live_enabled = live_cfg.live_enabled
        self._timeout = live_cfg.timeout_seconds
        self._max_retries = live_cfg.max_retries
        self._total_budget = (
            total_budget_seconds
            if total_budget_seconds is not None
            else GROK_TOTAL_BUDGET_SECONDS
        )
        self.last_failure_reason: GrokFailureReason | None = None

    async def analyze_sentiment(
        self,
        *,
        condition_id: str,
        market_title: str,
        market_category: MarketCategory,
        reference_timestamp_utc: str,
        tags: list[str] | None = None,
    ) -> SentimentResponse:
        """Fetch sentiment from Grok (or return mock/neutral fixture).

        On any failure (timeout, HTTP error, malformed JSON, schema error),
        returns NEUTRAL_SENTIMENT and logs the reason. Never raises.
        """
        self.last_failure_reason = None

        if self._mocked:
            logger.info(
                "grok_sentiment_mock",
                condition_id=condition_id,
                category=market_category.value,
            )
            return _MOCK_SENTIMENT

        if not self._live_enabled:
            self.last_failure_reason = GrokFailureReason.LIVE_DISABLED
            logger.info(
                "grok_sentiment_live_disabled",
                condition_id=condition_id,
                reason=GrokFailureReason.LIVE_DISABLED.value,
            )
            return NEUTRAL_SENTIMENT

        if not self._api_key.get_secret_value().strip():
            self.last_failure_reason = GrokFailureReason.MISSING_KEY
            logger.warning(
                "grok_sentiment_missing_key",
                condition_id=condition_id,
                reason=GrokFailureReason.MISSING_KEY.value,
            )
            return NEUTRAL_SENTIMENT

        if market_category not in (MarketCategory.CRYPTO, MarketCategory.POLITICS):
            self.last_failure_reason = GrokFailureReason.LIVE_DISABLED
            logger.info(
                "grok_sentiment_category_skipped",
                condition_id=condition_id,
                category=market_category.value,
                reason=GrokFailureReason.LIVE_DISABLED.value,
            )
            return NEUTRAL_SENTIMENT

        return await self._fetch_live(
            condition_id=condition_id,
            market_title=market_title,
            market_category=market_category,
            reference_timestamp_utc=reference_timestamp_utc,
            tags=tags,
        )

    # ── typed payload construction ──────────────────────────────────────────

    def _build_request(self, **kw: Any) -> GrokRequestEnvelope:
        """Build a typed live API request envelope."""
        condition_id = kw["condition_id"]
        market_title = kw["market_title"]
        market_category = kw["market_category"]
        reference_timestamp_utc = kw["reference_timestamp_utc"]
        tags = kw.get("tags")

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

        body = {
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

        return GrokRequestEnvelope(headers=headers, body=body)

    # ── live fetch with total budget + typed failure reasons ─────────────────

    async def _fetch_live(
        self,
        *,
        condition_id: str,
        market_title: str,
        market_category: MarketCategory,
        reference_timestamp_utc: str,
        tags: list[str] | None,
    ) -> SentimentResponse:
        """Live Grok API call with bounded retries, total budget, and per-attempt caps."""
        request = self._build_request(
            condition_id=condition_id,
            market_title=market_title,
            market_category=market_category,
            reference_timestamp_utc=reference_timestamp_utc,
            tags=tags,
        )

        last_error: str | None = None
        last_status: int | None = None
        t_start = time.monotonic()

        for attempt in range(self._max_retries + 1):
            elapsed = time.monotonic() - t_start
            remaining = self._total_budget - elapsed
            if remaining <= 0:
                self.last_failure_reason = GrokFailureReason.TIMEOUT
                logger.warning(
                    "grok_sentiment_budget_exhausted",
                    condition_id=condition_id,
                    elapsed_seconds=round(elapsed, 3),
                    total_budget=self._total_budget,
                    attempt=attempt,
                )
                return NEUTRAL_SENTIMENT

            try:
                return await self._attempt_live_call(
                    condition_id=condition_id,
                    request=request,
                    remaining_budget=remaining,
                )
            except asyncio.TimeoutError:
                last_error = "timeout"
                self.last_failure_reason = GrokFailureReason.TIMEOUT
                logger.warning(
                    "grok_sentiment_timeout",
                    condition_id=condition_id,
                    attempt=attempt,
                    max_retries=self._max_retries,
                    remaining_budget=round(remaining, 3),
                )
            except httpx.HTTPStatusError as exc:
                last_status = exc.response.status_code
                last_error = f"http_{last_status}"
                self.last_failure_reason = _http_status_to_failure_reason(last_status)
                logger.warning(
                    "grok_sentiment_http_error",
                    condition_id=condition_id,
                    attempt=attempt,
                    status_code=last_status,
                )
                if last_status in (401, 403, 400):
                    break
            except _SafetyRefusalError:
                last_error = "safety_refusal"
                self.last_failure_reason = GrokFailureReason.SAFETY_REFUSAL
                logger.warning(
                    "grok_sentiment_safety_refusal",
                    condition_id=condition_id,
                    attempt=attempt,
                )
                break
            except (ValidationError, KeyError, json.JSONDecodeError) as exc:
                last_error = "schema"
                self.last_failure_reason = GrokFailureReason.SCHEMA_ERROR
                logger.warning(
                    "grok_sentiment_schema_error",
                    condition_id=condition_id,
                    attempt=attempt,
                    error=str(exc),
                )
                break
            except Exception as exc:
                last_error = f"unexpected:{type(exc).__name__}"
                self.last_failure_reason = GrokFailureReason.UNEXPECTED
                logger.warning(
                    "grok_sentiment_unexpected_error",
                    condition_id=condition_id,
                    attempt=attempt,
                    error=str(exc),
                )

        logger.info(
            "grok_sentiment_fallback",
            condition_id=condition_id,
            last_error=last_error,
            last_status=last_status,
            total_attempts=self._max_retries + 1,
        )
        return NEUTRAL_SENTIMENT

    async def _attempt_live_call(
        self,
        *,
        condition_id: str,
        request: GrokRequestEnvelope,
        remaining_budget: float,
    ) -> SentimentResponse:
        """Single live Grok API call — per-attempt timeout capped at remaining budget."""
        per_attempt_timeout = min(self._timeout, max(remaining_budget, 0.1))
        client = self._http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(per_attempt_timeout),
        )
        try:
            resp = await asyncio.wait_for(
                client.post(
                    f"{self._base_url}/chat/completions",
                    json=request.body,
                    headers=request.headers,
                ),
                timeout=per_attempt_timeout,
            )
            resp.raise_for_status()
            body = resp.json()
            raw_text = body["choices"][0]["message"]["content"]

            # Detect safety refusal before attempting JSON parse
            _check_safety_refusal(raw_text)

            response_envelope = GrokResponseEnvelope(
                raw_text=raw_text,
                status_code=resp.status_code,
            )

            json_str = self._extract_json(response_envelope.raw_text)

            # Parse with Decimal to avoid float entering Pydantic boundary
            data = json.loads(json_str, parse_float=Decimal)
            return SentimentResponse.model_validate(data)

        finally:
            if self._http_client is None:
                await client.aclose()

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


class _SafetyRefusalError(Exception):
    """Raised when Grok API returns a content-filtering / safety refusal."""


_SAFETY_REFUSAL_PATTERNS = (
    "I cannot",
    "I can't",
    "I'm unable",
    "I am unable",
    "content policy",
    "safety guidelines",
    "against my guidelines",
    "not appropriate",
    "violates",
)


def _check_safety_refusal(text: str) -> None:
    """Raise _SafetyRefusalError if the response looks like a content-filtering refusal."""
    lower = text.lower()
    for pattern in _SAFETY_REFUSAL_PATTERNS:
        if pattern.lower() in lower:
            raise _SafetyRefusalError(
                f"Response matches safety refusal pattern: {pattern}"
            )


def _http_status_to_failure_reason(status: int) -> GrokFailureReason:
    if status == 401:
        return GrokFailureReason.HTTP_401
    if status == 403:
        return GrokFailureReason.HTTP_403
    if status == 429:
        return GrokFailureReason.HTTP_429
    if 500 <= status < 600:
        return GrokFailureReason.HTTP_5XX
    return GrokFailureReason.UNEXPECTED
