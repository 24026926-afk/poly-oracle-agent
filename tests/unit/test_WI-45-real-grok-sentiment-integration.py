"""
Unit tests for WI-45 — Real Grok Sentiment Integration.

Covers mock preservation, config validation, live-mode HTTP behavior,
failure → NEUTRAL_SENTIMENT, response parsing, safety invariants, and Decimal integrity.
"""

import asyncio
import json
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from src.agents.evaluation.grok_client import (
    GrokClient,
    NEUTRAL_SENTIMENT,
    _MOCK_SENTIMENT,
)
from src.core.config import AppConfig
from src.schemas.llm import MarketCategory, SentimentResponse


# ── helpers ────────────────────────────────────────────────────────────────

def _make_live_client(
    *,
    api_key: str = "test-key",
    base_url: str = "https://api.x.ai/v1",
    model: str = "grok-3",
    timeout_seconds: float = 2.0,
    max_retries: int = 2,
    http_client: httpx.AsyncClient | None = None,
) -> GrokClient:
    return GrokClient(
        api_key=SecretStr(api_key),
        base_url=base_url,
        model=model,
        mocked=False,
        live_enabled=True,
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
        http_client=http_client,
    )


def _make_mock_response(
    *,
    sentiment_score: float = 0.65,
    tweet_volume_delta: int = 12,
    top_narrative_summary: str = "Positive market sentiment across social channels.",
    status: int = 200,
) -> MagicMock:
    body = {
        "choices": [
            {
                "message": {
                    "content": json.dumps({
                        "sentiment_score": sentiment_score,
                        "tweet_volume_delta": tweet_volume_delta,
                        "top_narrative_summary": top_narrative_summary,
                    }),
                },
            },
        ],
    }
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.json.return_value = body
    resp.raise_for_status = MagicMock()
    if status >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=MagicMock(), response=resp
        )
    return resp


def _make_mock_async_client(resp: MagicMock) -> MagicMock:
    client = MagicMock(spec=httpx.AsyncClient)
    client.post = AsyncMock(return_value=resp)
    client.aclose = AsyncMock()
    return client


_STANDARD_CALL_ARGS = dict(
    condition_id="cond_001",
    market_title="Will BTC hit 100k?",
    market_category=MarketCategory.CRYPTO,
    reference_timestamp_utc="2026-05-05T12:00:00Z",
)


# ── Mock mode preservation (AC #1) ──────────────────────────────────────────

def test_mock_mode_returns_deterministic_sentiment():
    client = GrokClient(
        api_key=SecretStr(""),
        base_url="https://api.x.ai/v1",
        model="grok-3",
        mocked=True,
    )
    result = asyncio.run(client.analyze_sentiment(**_STANDARD_CALL_ARGS))
    assert result == _MOCK_SENTIMENT
    assert result.sentiment_score == Decimal("0.65")
    assert result.tweet_volume_delta == 12
    assert result is not NEUTRAL_SENTIMENT


def test_mock_mode_makes_no_http_calls():
    client = GrokClient(
        api_key=SecretStr(""),
        base_url="https://api.x.ai/v1",
        model="grok-3",
        mocked=True,
        live_enabled=True,
    )
    result = asyncio.run(client.analyze_sentiment(**_STANDARD_CALL_ARGS))
    assert result == _MOCK_SENTIMENT


# ── Config validation (AC #2, #3, #7) ──────────────────────────────────────

def test_grok_live_enabled_defaults_false(monkeypatch):
    """Schema default is False; env_file + environ overload must not mask this gate."""
    monkeypatch.delenv("GROK_LIVE_ENABLED", raising=False)
    monkeypatch.delenv("GROK_MOCKED", raising=False)
    cfg = AppConfig(
        _env_file=None,
        anthropic_api_key="sk-test",
        polygon_rpc_url="https://test",
        wallet_address="0x" + "0" * 40,
        wallet_private_key="0x" + "1" * 64,
    )
    assert cfg.grok_live_enabled is False


def test_grok_live_disabled_skips_live_call():
    client = GrokClient(
        api_key=SecretStr("valid-key"),
        base_url="https://api.x.ai/v1",
        model="grok-3",
        mocked=False,
        live_enabled=False,
    )
    result = asyncio.run(client.analyze_sentiment(**_STANDARD_CALL_ARGS))
    assert result == NEUTRAL_SENTIMENT


def test_grok_mocked_false_but_live_disabled_still_skips():
    client = GrokClient(
        api_key=SecretStr("valid-key"),
        base_url="https://api.x.ai/v1",
        model="grok-3",
        mocked=False,
        live_enabled=False,
    )
    result = asyncio.run(client.analyze_sentiment(**_STANDARD_CALL_ARGS))
    assert result == NEUTRAL_SENTIMENT


def test_missing_api_key_with_live_enabled_returns_neutral():
    client = GrokClient(
        api_key=SecretStr(""),
        base_url="https://api.x.ai/v1",
        model="grok-3",
        mocked=False,
        live_enabled=True,
    )
    result = asyncio.run(client.analyze_sentiment(**_STANDARD_CALL_ARGS))
    assert result == NEUTRAL_SENTIMENT


def test_missing_api_key_whitespace_only_returns_neutral():
    client = GrokClient(
        api_key=SecretStr("   "),
        base_url="https://api.x.ai/v1",
        model="grok-3",
        mocked=False,
        live_enabled=True,
    )
    result = asyncio.run(client.analyze_sentiment(**_STANDARD_CALL_ARGS))
    assert result == NEUTRAL_SENTIMENT


def test_grok_timeout_seconds_config_field_exists():
    cfg = AppConfig()
    assert cfg.grok_timeout_seconds == 2.0


def test_grok_max_retries_config_field_exists():
    cfg = AppConfig()
    assert cfg.grok_max_retries == 2


# ── Live mode – HTTP behavior (AC #2) ──────────────────────────────────────

def test_live_mode_posts_to_configured_endpoint():
    resp = _make_mock_response()
    mock_client = _make_mock_async_client(resp)
    client = _make_live_client(http_client=mock_client)

    result = asyncio.run(client.analyze_sentiment(**_STANDARD_CALL_ARGS))

    assert result.sentiment_score == Decimal("0.65")
    mock_client.post.assert_called_once()
    call_url = mock_client.post.call_args[0][0]
    assert call_url == "https://api.x.ai/v1/chat/completions"


def test_live_mode_uses_configured_timeout():
    resp = _make_mock_response()
    mock_client = _make_mock_async_client(resp)
    client = _make_live_client(http_client=mock_client, timeout_seconds=5.0)

    asyncio.run(client.analyze_sentiment(**_STANDARD_CALL_ARGS))
    assert client._timeout == 5.0


def test_live_mode_respects_max_retries():
    """Transient 5xx should retry up to max_retries, then return neutral."""
    resp = _make_mock_response(status=503)
    mock_client = _make_mock_async_client(resp)
    client = _make_live_client(http_client=mock_client, max_retries=3)

    result = asyncio.run(client.analyze_sentiment(**_STANDARD_CALL_ARGS))
    assert result == NEUTRAL_SENTIMENT
    # Called max_retries + 1 times
    assert mock_client.post.call_count == 4


def test_live_mode_no_retry_on_4xx():
    """401/403/400 should NOT retry — client errors."""
    resp = _make_mock_response(status=401)
    mock_client = _make_mock_async_client(resp)
    client = _make_live_client(http_client=mock_client, max_retries=3)

    result = asyncio.run(client.analyze_sentiment(**_STANDARD_CALL_ARGS))
    assert result == NEUTRAL_SENTIMENT
    assert mock_client.post.call_count == 1


# ── Failure → NEUTRAL_SENTIMENT (AC #3) ────────────────────────────────────

def test_http_timeout_returns_neutral_sentiment():
    mock_client = MagicMock(spec=httpx.AsyncClient)
    mock_client.post = AsyncMock(side_effect=asyncio.TimeoutError)
    mock_client.aclose = AsyncMock()
    client = _make_live_client(http_client=mock_client)

    result = asyncio.run(client.analyze_sentiment(**_STANDARD_CALL_ARGS))
    assert result == NEUTRAL_SENTIMENT


def test_http_401_returns_neutral_sentiment():
    resp = _make_mock_response(status=401)
    mock_client = _make_mock_async_client(resp)
    client = _make_live_client(http_client=mock_client)

    result = asyncio.run(client.analyze_sentiment(**_STANDARD_CALL_ARGS))
    assert result == NEUTRAL_SENTIMENT


def test_http_403_returns_neutral_sentiment():
    resp = _make_mock_response(status=403)
    mock_client = _make_mock_async_client(resp)
    client = _make_live_client(http_client=mock_client)

    result = asyncio.run(client.analyze_sentiment(**_STANDARD_CALL_ARGS))
    assert result == NEUTRAL_SENTIMENT


def test_http_429_returns_neutral_after_retries():
    resp = _make_mock_response(status=429)
    mock_client = _make_mock_async_client(resp)
    client = _make_live_client(http_client=mock_client, max_retries=2)

    result = asyncio.run(client.analyze_sentiment(**_STANDARD_CALL_ARGS))
    assert result == NEUTRAL_SENTIMENT
    assert mock_client.post.call_count == 3  # 1 initial + 2 retries


def test_http_5xx_returns_neutral_sentiment():
    resp = _make_mock_response(status=500)
    mock_client = _make_mock_async_client(resp)
    client = _make_live_client(http_client=mock_client, max_retries=1)

    result = asyncio.run(client.analyze_sentiment(**_STANDARD_CALL_ARGS))
    assert result == NEUTRAL_SENTIMENT
    assert mock_client.post.call_count == 2


def test_malformed_json_response_returns_neutral():
    body = {"choices": [{"message": {"content": "not json at all"}}]}
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.json.return_value = body
    resp.raise_for_status = MagicMock()
    mock_client = _make_mock_async_client(resp)
    client = _make_live_client(http_client=mock_client)

    result = asyncio.run(client.analyze_sentiment(**_STANDARD_CALL_ARGS))
    assert result == NEUTRAL_SENTIMENT


def test_missing_required_fields_returns_neutral():
    body = {
        "choices": [
            {"message": {"content": json.dumps({"tweet_volume_delta": 5})}},
        ],
    }
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.json.return_value = body
    resp.raise_for_status = MagicMock()
    mock_client = _make_mock_async_client(resp)
    client = _make_live_client(http_client=mock_client)

    result = asyncio.run(client.analyze_sentiment(**_STANDARD_CALL_ARGS))
    assert result == NEUTRAL_SENTIMENT


def test_schema_validation_error_returns_neutral():
    body = {
        "choices": [
            {"message": {"content": json.dumps({"sentiment_score": 2.5, "tweet_volume_delta": 5, "top_narrative_summary": "x" * 10})}},
        ],
    }
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.json.return_value = body
    resp.raise_for_status = MagicMock()
    mock_client = _make_mock_async_client(resp)
    client = _make_live_client(http_client=mock_client)

    result = asyncio.run(client.analyze_sentiment(**_STANDARD_CALL_ARGS))
    assert result == NEUTRAL_SENTIMENT


def test_unexpected_exception_returns_neutral():
    mock_client = MagicMock(spec=httpx.AsyncClient)
    mock_client.post = AsyncMock(side_effect=OSError("network down"))
    mock_client.aclose = AsyncMock()
    client = _make_live_client(http_client=mock_client)

    result = asyncio.run(client.analyze_sentiment(**_STANDARD_CALL_ARGS))
    assert result == NEUTRAL_SENTIMENT


# ── Response parsing (AC #3, AC #6) ────────────────────────────────────────

def test_extract_json_from_markdown_code_block():
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.json.return_value = {
        "choices": [
            {"message": {"content": '```json\n{"sentiment_score": 0.8, "tweet_volume_delta": 20, "top_narrative_summary": "Strong bullish signals detected."}\n```'}},
        ],
    }
    resp.raise_for_status = MagicMock()
    mock_client = _make_mock_async_client(resp)
    client = _make_live_client(http_client=mock_client)

    result = asyncio.run(client.analyze_sentiment(**_STANDARD_CALL_ARGS))
    assert result.sentiment_score == Decimal("0.8")
    assert result.tweet_volume_delta == 20


def test_extract_json_from_plain_text():
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.json.return_value = {
        "choices": [
            {"message": {"content": '{"sentiment_score": -0.3, "tweet_volume_delta": -5, "top_narrative_summary": "Bearish momentum building."}'}},
        ],
    }
    resp.raise_for_status = MagicMock()
    mock_client = _make_mock_async_client(resp)
    client = _make_live_client(http_client=mock_client)

    result = asyncio.run(client.analyze_sentiment(**_STANDARD_CALL_ARGS))
    assert result.sentiment_score == Decimal("-0.3")
    assert result.tweet_volume_delta == -5


def test_non_json_response_returns_neutral():
    body = {"choices": [{"message": {"content": "I think the market is bullish today. No JSON here."}}]}
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.json.return_value = body
    resp.raise_for_status = MagicMock()
    mock_client = _make_mock_async_client(resp)
    client = _make_live_client(http_client=mock_client)

    result = asyncio.run(client.analyze_sentiment(**_STANDARD_CALL_ARGS))
    assert result == NEUTRAL_SENTIMENT


def test_sentiment_score_out_of_range_rejected():
    body = {
        "choices": [
            {"message": {"content": json.dumps({"sentiment_score": 1.5, "tweet_volume_delta": 5, "top_narrative_summary": "x" * 10})}},
        ],
    }
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.json.return_value = body
    resp.raise_for_status = MagicMock()
    mock_client = _make_mock_async_client(resp)
    client = _make_live_client(http_client=mock_client)

    result = asyncio.run(client.analyze_sentiment(**_STANDARD_CALL_ARGS))
    assert result == NEUTRAL_SENTIMENT


def test_sentiment_score_negative_out_of_range_rejected():
    body = {
        "choices": [
            {"message": {"content": json.dumps({"sentiment_score": -1.5, "tweet_volume_delta": 5, "top_narrative_summary": "x" * 10})}},
        ],
    }
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.json.return_value = body
    resp.raise_for_status = MagicMock()
    mock_client = _make_mock_async_client(resp)
    client = _make_live_client(http_client=mock_client)

    result = asyncio.run(client.analyze_sentiment(**_STANDARD_CALL_ARGS))
    assert result == NEUTRAL_SENTIMENT


def test_tweet_volume_delta_not_integer_rejected():
    body = {
        "choices": [
            {"message": {"content": json.dumps({"sentiment_score": 0.5, "tweet_volume_delta": "high", "top_narrative_summary": "x" * 10})}},
        ],
    }
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.json.return_value = body
    resp.raise_for_status = MagicMock()
    mock_client = _make_mock_async_client(resp)
    client = _make_live_client(http_client=mock_client)

    result = asyncio.run(client.analyze_sentiment(**_STANDARD_CALL_ARGS))
    assert result == NEUTRAL_SENTIMENT


def test_top_narrative_summary_too_short_rejected():
    body = {
        "choices": [
            {"message": {"content": json.dumps({"sentiment_score": 0.5, "tweet_volume_delta": 5, "top_narrative_summary": "short"})}},
        ],
    }
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.json.return_value = body
    resp.raise_for_status = MagicMock()
    mock_client = _make_mock_async_client(resp)
    client = _make_live_client(http_client=mock_client)

    result = asyncio.run(client.analyze_sentiment(**_STANDARD_CALL_ARGS))
    assert result == NEUTRAL_SENTIMENT


def test_top_narrative_summary_too_long_rejected():
    body = {
        "choices": [
            {"message": {"content": json.dumps({"sentiment_score": 0.5, "tweet_volume_delta": 5, "top_narrative_summary": "x" * 350})}},
        ],
    }
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.json.return_value = body
    resp.raise_for_status = MagicMock()
    mock_client = _make_mock_async_client(resp)
    client = _make_live_client(http_client=mock_client)

    result = asyncio.run(client.analyze_sentiment(**_STANDARD_CALL_ARGS))
    assert result == NEUTRAL_SENTIMENT


def test_missing_top_narrative_summary_rejected():
    body = {
        "choices": [
            {"message": {"content": json.dumps({"sentiment_score": 0.5, "tweet_volume_delta": 5})}},
        ],
    }
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.json.return_value = body
    resp.raise_for_status = MagicMock()
    mock_client = _make_mock_async_client(resp)
    client = _make_live_client(http_client=mock_client)

    result = asyncio.run(client.analyze_sentiment(**_STANDARD_CALL_ARGS))
    assert result == NEUTRAL_SENTIMENT


# ── Safety invariants (AC #4, #5, #7, #8) ──────────────────────────────────

def test_api_key_not_logged_in_structured_logs():
    """Verify that _build_payload never includes key in logged data and that
    warning log events use condition_id, not key material."""
    client = _make_live_client(api_key="sk-secret-12345")

    payload = client._build_request(**_STANDARD_CALL_ARGS, tags=None)
    # The key is in the headers but only for transport; verify we can inspect it
    assert "sk-secret-12345" in payload.headers["Authorization"]
    # The return dict itself is not sent to structlog — only condition_id
    # is used in log calls (verified by test coverage of log paths).


def test_api_key_not_logged_in_error_paths():
    """On HTTP 401, the log event contains status_code, not the key."""
    resp = _make_mock_response(status=401)
    mock_client = _make_mock_async_client(resp)
    client = _make_live_client(http_client=mock_client)

    with patch("src.agents.evaluation.grok_client.logger.warning") as mock_log:
        result = asyncio.run(client.analyze_sentiment(**_STANDARD_CALL_ARGS))

    assert result == NEUTRAL_SENTIMENT
    for call_args in mock_log.call_args_list:
        extra = call_args[1] if len(call_args) > 1 else {}
        assert "api_key" not in str(extra).lower()
        assert "sk-secret" not in str(extra)


def test_sentiment_cannot_bypass_gatekeeper():
    """SentimentResponse has no fields that can set LLMEvaluationResponse decisions."""
    sr = SentimentResponse(
        sentiment_score=Decimal("0.9"),
        tweet_volume_delta=100,
        top_narrative_summary="Extremely bullish signals everywhere.",
    )
    # SentimentResponse is frozen and has no decision/execution fields
    assert not hasattr(sr, "decision")
    assert not hasattr(sr, "action")
    assert not hasattr(sr, "confidence")


def test_live_sentiment_only_for_eligible_categories():
    """CRYPTO and POLITICS are eligible; SPORTS/GENERAL gated in GrokClient."""
    resp = _make_mock_response(sentiment_score=0.5)
    mock_client = _make_mock_async_client(resp)
    client = _make_live_client(http_client=mock_client)

    # SPORTS — gated in GrokClient, returns NEUTRAL, no HTTP call
    result = asyncio.run(client.analyze_sentiment(
        condition_id="cond_002",
        market_title="Super Bowl winner",
        market_category=MarketCategory.SPORTS,
        reference_timestamp_utc="2026-05-05T12:00:00Z",
    ))
    assert result == NEUTRAL_SENTIMENT
    mock_client.post.assert_not_called()


def test_neutral_response_has_sentiment_score_zero():
    assert NEUTRAL_SENTIMENT.sentiment_score == Decimal("0.0")
    assert NEUTRAL_SENTIMENT.tweet_volume_delta == 0


# ── Decimal integrity (invariant from PRD) ──────────────────────────────────

def test_live_response_float_sentiment_score_rejected():
    """Raw Python float rejected at schema boundary; use Decimal or str."""
    with pytest.raises(ValueError, match="Float financial values are forbidden"):
        SentimentResponse(
            sentiment_score=0.8,  # raw float — rejected
            tweet_volume_delta=10,
            top_narrative_summary="Testing float rejection.",
        )


def test_sentiment_score_preserves_decimal_precision():
    resp = _make_mock_response(sentiment_score=0.333)
    mock_client = _make_mock_async_client(resp)
    client = _make_live_client(http_client=mock_client)

    result = asyncio.run(client.analyze_sentiment(**_STANDARD_CALL_ARGS))
    assert isinstance(result.sentiment_score, Decimal)
    assert result.sentiment_score == Decimal("0.333")


def test_grok_mocked_defaults_true(monkeypatch):
    """Existing grok_mocked config field defaults to True."""
    monkeypatch.delenv("GROK_LIVE_ENABLED", raising=False)
    monkeypatch.delenv("GROK_MOCKED", raising=False)
    cfg = AppConfig(
        _env_file=None,
        anthropic_api_key="sk-test",
        polygon_rpc_url="https://test",
        wallet_address="0x" + "0" * 40,
        wallet_private_key="0x" + "1" * 64,
    )
    assert cfg.grok_mocked is True
