"""
Integration tests for WI-45 — Real Grok Sentiment Integration.

Tests the full live Grok sentiment pipeline with mocked HTTP, verifying
end-to-end category gating, retry behavior, and ClaudeClient audit trail.
"""

import asyncio
import json
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from pydantic import SecretStr

from src.agents.evaluation.claude_client import ClaudeClient, NEUTRAL_SENTIMENT
from src.agents.evaluation.grok_client import GrokClient, _MOCK_SENTIMENT
from src.schemas.llm import GrokFailureReason, MarketCategory, SentimentResponse


# ── helpers ────────────────────────────────────────────────────────────────

def _make_mock_httpx_response(*, status: int = 200, content: str = "") -> MagicMock:
    body = {
        "choices": [
            {"message": {"content": content}},
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


def _valid_sentiment_json() -> str:
    return json.dumps({
        "sentiment_score": 0.72,
        "tweet_volume_delta": 25,
        "top_narrative_summary": "Growing bullish momentum across crypto channels.",
    })


def _neutral_sentiment_json() -> str:
    return json.dumps({
        "sentiment_score": 0.0,
        "tweet_volume_delta": 0,
        "top_narrative_summary": "No significant sentiment detected in the last 60 minutes.",
    })


def _make_live_client(*, max_retries: int = 2) -> GrokClient:
    return GrokClient(
        api_key=SecretStr("sk-test-integration"),
        base_url="https://api.x.ai/v1",
        model="grok-3",
        mocked=False,
        live_enabled=True,
        timeout_seconds=2.0,
        max_retries=max_retries,
    )


_STANDARD_ARGS = dict(
    condition_id="cond_int_001",
    market_title="Will BTC break 100k?",
    market_category=MarketCategory.CRYPTO,
    reference_timestamp_utc="2026-05-05T12:00:00Z",
)


# ── integration: GrokClient live path ──────────────────────────────────────

@pytest.mark.asyncio
async def test_full_live_flow_returns_parsed_sentiment():
    """End-to-end live flow: mocked HTTP → valid JSON → SentimentResponse."""
    resp = _make_mock_httpx_response(status=200, content=_valid_sentiment_json())
    mock_http = MagicMock(spec=httpx.AsyncClient)
    mock_http.post = AsyncMock(return_value=resp)
    mock_http.aclose = AsyncMock()

    client = GrokClient(
        api_key=SecretStr("sk-test"),
        base_url="https://api.x.ai/v1",
        model="grok-3",
        mocked=False,
        live_enabled=True,
        http_client=mock_http,
    )

    result = await client.analyze_sentiment(**_STANDARD_ARGS)

    assert isinstance(result, SentimentResponse)
    assert result.sentiment_score == Decimal("0.72")
    assert result.tweet_volume_delta == 25
    assert result is not NEUTRAL_SENTIMENT


@pytest.mark.asyncio
async def test_retry_on_transient_then_succeed():
    """First attempt 503, second attempt 200 → success."""
    fail_resp = _make_mock_httpx_response(status=503)
    ok_resp = _make_mock_httpx_response(status=200, content=_valid_sentiment_json())

    mock_http = MagicMock(spec=httpx.AsyncClient)
    mock_http.post = AsyncMock(side_effect=[fail_resp, ok_resp])
    mock_http.aclose = AsyncMock()

    client = _make_live_client(max_retries=2)
    client._http_client = mock_http

    result = await client.analyze_sentiment(**_STANDARD_ARGS)
    assert result.sentiment_score == Decimal("0.72")
    assert mock_http.post.call_count == 2


@pytest.mark.asyncio
async def test_category_sports_gated_in_grok_client():
    """SPORTS category → GrokClient returns NEUTRAL_SENTIMENT directly."""
    resp = _make_mock_httpx_response(status=200, content=_valid_sentiment_json())
    mock_http = MagicMock(spec=httpx.AsyncClient)
    mock_http.post = AsyncMock(return_value=resp)
    mock_http.aclose = AsyncMock()

    client = _make_live_client()
    client._http_client = mock_http

    result = await client.analyze_sentiment(
        condition_id="cond_sports_001",
        market_title="Super Bowl Winner",
        market_category=MarketCategory.SPORTS,
        reference_timestamp_utc="2026-05-05T12:00:00Z",
    )

    assert result == NEUTRAL_SENTIMENT
    mock_http.post.assert_not_called()


@pytest.mark.asyncio
async def test_category_general_gated_in_grok_client():
    """GENERAL category → GrokClient returns NEUTRAL_SENTIMENT directly."""
    mock_http = MagicMock(spec=httpx.AsyncClient)
    mock_http.post = AsyncMock()
    mock_http.aclose = AsyncMock()

    client = _make_live_client()
    client._http_client = mock_http

    result = await client.analyze_sentiment(
        condition_id="cond_general_001",
        market_title="Will it rain tomorrow?",
        market_category=MarketCategory.GENERAL,
        reference_timestamp_utc="2026-05-05T12:00:00Z",
    )

    assert result == NEUTRAL_SENTIMENT
    mock_http.post.assert_not_called()


@pytest.mark.asyncio
async def test_claude_client_audit_trail_neutral_fallback():
    """ClaudeClient._fetch_sentiment logs FALLBACK/NEUTRAL_RETURNED for neutral results."""
    cfg = MagicMock()
    cfg.dry_run = True
    cfg.anthropic_api_key = MagicMock()
    cfg.anthropic_api_key.get_secret_value.return_value = "sk-test"
    cfg.anthropic_model = "claude-test"
    cfg.grok_api_key = MagicMock()
    cfg.grok_api_key.get_secret_value.return_value = ""
    cfg.grok_base_url = "https://api.x.ai/v1"
    cfg.grok_model = "grok-3"
    cfg.grok_mocked = False
    cfg.grok_live_enabled = True
    cfg.grok_timeout_seconds = 2.0
    cfg.grok_max_retries = 2
    cfg.clob_rest_url = "http://localhost"

    in_q: asyncio.Queue = asyncio.Queue()
    out_q: asyncio.Queue = asyncio.Queue()
    client = ClaudeClient(in_queue=in_q, out_queue=out_q, config=cfg)

    with patch.object(client, "_log_sentiment") as mock_log:
        result = await client._fetch_sentiment(
            category=MarketCategory.POLITICS,
            market_state={
                "condition_id": "cond_audit_001",
                "title": "Election 2026",
                "timestamp": 1700000000,
            },
            snapshot_id="snap_audit_001",
        )

    assert result == NEUTRAL_SENTIMENT
    # Should have logged FALLBACK (missing key → GrokClient returns NEUTRAL → ClaudeClient detects it)
    mock_log.assert_called_once()
    call_kwargs = mock_log.call_args[1]
    assert call_kwargs["status"] == "FALLBACK"
    assert call_kwargs["reason"] == "missing_key"  # typed GrokFailureReason from GrokClient


# ── integration: GrokFailureReason via GrokClient log path ─────────────────

@pytest.mark.asyncio
async def test_grok_client_uses_typed_failure_reason_missing_key():
    """Missing API key → log event uses GrokFailureReason.MISSING_KEY."""
    client = GrokClient(
        api_key=SecretStr(""),
        base_url="https://api.x.ai/v1",
        model="grok-3",
        mocked=False,
        live_enabled=True,
    )
    with patch("src.agents.evaluation.grok_client.logger.warning") as mock_log:
        result = await client.analyze_sentiment(**_STANDARD_ARGS)

    assert result == NEUTRAL_SENTIMENT
    mock_log.assert_called_once()
    assert mock_log.call_args[1]["reason"] == GrokFailureReason.MISSING_KEY.value


# ── integration: mock mode still works ─────────────────────────────────────

@pytest.mark.asyncio
async def test_mock_mode_integration():
    """Mock mode returns _MOCK_SENTIMENT for any category without HTTP."""
    client = GrokClient(
        api_key=SecretStr(""),
        base_url="https://api.x.ai/v1",
        model="grok-3",
        mocked=True,
    )
    result = await client.analyze_sentiment(**_STANDARD_ARGS)
    assert result == _MOCK_SENTIMENT

    # Also works for SPORTS in mock mode
    result2 = await client.analyze_sentiment(
        condition_id="cond_sports_002",
        market_title="NBA Finals",
        market_category=MarketCategory.SPORTS,
        reference_timestamp_utc="2026-05-05T12:00:00Z",
    )
    assert result2 == _MOCK_SENTIMENT


# ── integration: SentimentResponse float rejection ─────────────────────────

def test_sentiment_response_rejects_raw_float():
    """SentimentResponse._parse_decimal rejects raw Python float at boundary."""
    with pytest.raises(ValueError, match="Float financial values are forbidden"):
        SentimentResponse(
            sentiment_score=0.5,  # raw float — rejected
            tweet_volume_delta=10,
            top_narrative_summary="Testing float rejection.",
        )


def test_sentiment_response_accepts_decimal():
    """SentimentResponse accepts Decimal directly."""
    sr = SentimentResponse(
        sentiment_score=Decimal("0.5"),
        tweet_volume_delta=10,
        top_narrative_summary="Testing Decimal acceptance.",
    )
    assert sr.sentiment_score == Decimal("0.5")


def test_sentiment_response_accepts_str():
    """SentimentResponse accepts string (from JSON parsing)."""
    sr = SentimentResponse(
        sentiment_score="0.5",
        tweet_volume_delta=10,
        top_narrative_summary="Testing string acceptance.",
    )
    assert sr.sentiment_score == Decimal("0.5")
