"""
tests/integration/test_sentiment_chain.py

Integration tests for WI-12 — Chained Prompt Factory & Grok Sentiment.

Tests use the ACTUAL GrokClient instantiated in ClaudeClient.__init__
(mock mode via AppConfig.grok_mocked=True). Failure scenarios use
patch.object on the real instance to inject timeouts / errors.

Asserts:
  - CRYPTO / POLITICS trigger GrokClient.analyze_sentiment (mock mode)
  - SPORTS / GENERAL bypass Grok entirely
  - Grok timeout -> neutral fallback, pipeline continues
  - Malformed Grok JSON -> neutral fallback, pipeline continues
  - PromptFactory injects sentiment block into evaluation prompt
  - LLMEvaluationResponse remains terminal Gatekeeper gate
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.agents.evaluation.claude_client import ClaudeClient
from src.agents.evaluation.grok_client import GrokClient, NEUTRAL_SENTIMENT, _MOCK_SENTIMENT
from tests.conftest import APPROVED_REFLECTION_JSON


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_anthropic_response(raw_json: str):
    """Build a mock Anthropic message response object."""
    content_block = MagicMock()
    content_block.text = raw_json

    usage = MagicMock()
    usage.input_tokens = 150
    usage.output_tokens = 350

    resp = MagicMock()
    resp.content = [content_block]
    resp.usage = usage
    return resp


def _crypto_market_item() -> dict:
    """Market item that routes to CRYPTO via keyword matching."""
    return {
        "snapshot_id": "snap-crypto-001",
        "yes_token_id": "tok-yes-001",
        "state": {
            "condition_id": "0xaaaa1111bbbb2222cccc3333dddd4444eeee5555",
            "title": "Will Bitcoin exceed $100k by July?",
            "tags": ["btc", "crypto"],
            "best_bid": 0.45,
            "best_ask": 0.455,
            "midpoint": 0.4525,
            "spread": 0.005,
            "timestamp": 1700000000,
        },
    }


def _politics_market_item() -> dict:
    """Market item that routes to POLITICS via keyword matching."""
    return {
        "snapshot_id": "snap-politics-001",
        "yes_token_id": "tok-yes-002",
        "state": {
            "condition_id": "0xbbbb2222cccc3333dddd4444eeee5555ffff6666",
            "title": "Will the president win the election?",
            "tags": ["election", "vote"],
            "best_bid": 0.55,
            "best_ask": 0.56,
            "midpoint": 0.555,
            "spread": 0.01,
            "timestamp": 1700000000,
        },
    }


def _sports_market_item() -> dict:
    """Market item that routes to SPORTS via keyword matching."""
    return {
        "snapshot_id": "snap-sports-001",
        "yes_token_id": "tok-yes-003",
        "state": {
            "condition_id": "0xcccc3333dddd4444eeee5555ffff6666aaaa7777",
            "title": "Will the NBA finals go to game 7?",
            "tags": ["nba", "basketball"],
            "best_bid": 0.40,
            "best_ask": 0.41,
            "midpoint": 0.405,
            "spread": 0.01,
            "timestamp": 1700000000,
        },
    }


def _general_market_item() -> dict:
    """Market item that routes to GENERAL (no domain keywords)."""
    return {
        "snapshot_id": "snap-general-001",
        "yes_token_id": "tok-yes-004",
        "state": {
            "condition_id": "0xdddd4444eeee5555ffff6666aaaa7777bbbb8888",
            "title": "Will the new product launch succeed?",
            "tags": [],
            "best_bid": 0.50,
            "best_ask": 0.51,
            "midpoint": 0.505,
            "spread": 0.01,
            "timestamp": 1700000000,
        },
    }


def _setup_client(test_config, mock_anthropic_buy_json, *, extra_side_effects=None):
    """Create a ClaudeClient with real GrokClient and mocked Anthropic API.

    Provides side_effect responses for both the primary evaluation call
    and the WI-13 reflection audit call.
    """
    in_q: asyncio.Queue = asyncio.Queue()
    out_q: asyncio.Queue = asyncio.Queue()
    client = ClaudeClient(in_queue=in_q, out_queue=out_q, config=test_config)

    # Verify real GrokClient was instantiated by __init__
    assert isinstance(client._grok_client, GrokClient)

    # Mock Anthropic API — primary eval + approved reflection
    side_effects = extra_side_effects or [
        _mock_anthropic_response(mock_anthropic_buy_json),
        _mock_anthropic_response(APPROVED_REFLECTION_JSON),
    ]
    client.client = MagicMock()
    client.client.messages.create = AsyncMock(side_effect=side_effects)
    client._persist_decision = AsyncMock()

    return client, in_q, out_q


# ---------------------------------------------------------------------------
# Test 1: CRYPTO triggers Grok call (real GrokClient, mock mode)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_crypto_triggers_grok_sentiment_call(
    test_config, mock_anthropic_buy_json, mock_polymarket,
):
    """CRYPTO market MUST trigger GrokClient — prompt contains mock sentiment values."""
    client, _, out_q = _setup_client(test_config, mock_anthropic_buy_json)

    with patch.object(
        client._grok_client, "analyze_sentiment", wraps=client._grok_client.analyze_sentiment,
    ) as spy:
        await client._process_evaluation(_crypto_market_item())
        spy.assert_called_once()

    # Prompt must contain _MOCK_SENTIMENT values (score=0.65, delta=12)
    # Use call_args_list[0] to get the primary eval call (not the reflection call)
    call_args = client.client.messages.create.call_args_list[0]
    messages = call_args.kwargs.get("messages") or call_args[1].get("messages")
    prompt_text = messages[0]["content"]
    assert str(_MOCK_SENTIMENT.sentiment_score) in prompt_text
    assert str(_MOCK_SENTIMENT.tweet_volume_delta) in prompt_text


# ---------------------------------------------------------------------------
# Test 2: POLITICS triggers Grok call (real GrokClient, mock mode)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_politics_triggers_grok_sentiment_call(
    test_config, mock_anthropic_buy_json, mock_polymarket,
):
    """POLITICS market MUST trigger GrokClient — prompt contains mock sentiment values."""
    client, _, out_q = _setup_client(test_config, mock_anthropic_buy_json)

    with patch.object(
        client._grok_client, "analyze_sentiment", wraps=client._grok_client.analyze_sentiment,
    ) as spy:
        await client._process_evaluation(_politics_market_item())
        spy.assert_called_once()

    call_args = client.client.messages.create.call_args_list[0]
    messages = call_args.kwargs.get("messages") or call_args[1].get("messages")
    prompt_text = messages[0]["content"]
    assert str(_MOCK_SENTIMENT.sentiment_score) in prompt_text


# ---------------------------------------------------------------------------
# Test 3: SPORTS skips Grok (real GrokClient, never called)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sports_skips_grok_sentiment(
    test_config, mock_anthropic_buy_json, mock_polymarket,
):
    """SPORTS market MUST NOT call GrokClient — prompt contains neutral fallback."""
    client, _, _ = _setup_client(test_config, mock_anthropic_buy_json)

    with patch.object(
        client._grok_client, "analyze_sentiment", wraps=client._grok_client.analyze_sentiment,
    ) as spy:
        await client._process_evaluation(_sports_market_item())
        spy.assert_not_called()

    # Prompt must contain neutral fallback values
    call_args = client.client.messages.create.call_args_list[0]
    messages = call_args.kwargs.get("messages") or call_args[1].get("messages")
    prompt_text = messages[0]["content"]
    assert str(NEUTRAL_SENTIMENT.sentiment_score) in prompt_text
    assert "neutral" in prompt_text.lower()


# ---------------------------------------------------------------------------
# Test 4: GENERAL skips Grok (real GrokClient, never called)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_general_skips_grok_sentiment(
    test_config, mock_anthropic_buy_json, mock_polymarket,
):
    """GENERAL market MUST NOT call GrokClient — prompt contains neutral fallback."""
    client, _, _ = _setup_client(test_config, mock_anthropic_buy_json)

    with patch.object(
        client._grok_client, "analyze_sentiment", wraps=client._grok_client.analyze_sentiment,
    ) as spy:
        await client._process_evaluation(_general_market_item())
        spy.assert_not_called()

    call_args = client.client.messages.create.call_args_list[0]
    messages = call_args.kwargs.get("messages") or call_args[1].get("messages")
    prompt_text = messages[0]["content"]
    assert str(NEUTRAL_SENTIMENT.sentiment_score) in prompt_text


# ---------------------------------------------------------------------------
# Test 5: Grok timeout -> neutral fallback, evaluation continues
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_grok_timeout_falls_back_to_neutral_sentiment(
    test_config, mock_anthropic_buy_json, mock_polymarket,
):
    """When Grok times out, pipeline continues with neutral sentiment."""
    client, _, _ = _setup_client(test_config, mock_anthropic_buy_json)

    # Patch analyze_sentiment on the real GrokClient to raise TimeoutError
    with patch.object(
        client._grok_client, "analyze_sentiment", new_callable=AsyncMock,
        side_effect=asyncio.TimeoutError,
    ) as patched:
        await client._process_evaluation(_crypto_market_item())
        patched.assert_called_once()

    # Evaluation still completed (Anthropic API was called with neutral fallback)
    assert client.client.messages.create.call_count >= 1
    call_args = client.client.messages.create.call_args_list[0]
    messages = call_args.kwargs.get("messages") or call_args[1].get("messages")
    prompt_text = messages[0]["content"]
    assert "### SENTIMENT ORACLE (LAST 60 MIN)" in prompt_text
    assert "neutral" in prompt_text.lower()


# ---------------------------------------------------------------------------
# Test 6: Malformed Grok JSON -> neutral fallback, evaluation continues
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_malformed_grok_json_falls_back_to_neutral(
    test_config, mock_anthropic_buy_json, mock_polymarket,
):
    """When Grok returns invalid data, pipeline continues with neutral sentiment."""
    client, _, _ = _setup_client(test_config, mock_anthropic_buy_json)

    with patch.object(
        client._grok_client, "analyze_sentiment", new_callable=AsyncMock,
        side_effect=Exception("Grok returned malformed JSON"),
    ) as patched:
        await client._process_evaluation(_crypto_market_item())
        patched.assert_called_once()

    assert client.client.messages.create.call_count >= 1
    call_args = client.client.messages.create.call_args_list[0]
    messages = call_args.kwargs.get("messages") or call_args[1].get("messages")
    prompt_text = messages[0]["content"]
    assert "### SENTIMENT ORACLE (LAST 60 MIN)" in prompt_text
    assert "neutral" in prompt_text.lower()


# ---------------------------------------------------------------------------
# Test 7: Prompt includes sentiment block values (real mock-mode client)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_prompt_includes_sentiment_oracle_block(
    test_config, mock_anthropic_buy_json, mock_polymarket,
):
    """The evaluation prompt sent to Claude must contain the sentiment oracle section
    with actual _MOCK_SENTIMENT values from the real GrokClient."""
    client, _, _ = _setup_client(test_config, mock_anthropic_buy_json)

    await client._process_evaluation(_crypto_market_item())

    call_args = client.client.messages.create.call_args_list[0]
    messages = call_args.kwargs.get("messages") or call_args[1].get("messages")
    prompt_text = messages[0]["content"]

    # Prompt must include the sentinel header
    assert "### SENTIMENT ORACLE (LAST 60 MIN)" in prompt_text
    # Prompt must include actual mock sentiment values
    assert str(_MOCK_SENTIMENT.sentiment_score) in prompt_text
    assert str(_MOCK_SENTIMENT.tweet_volume_delta) in prompt_text
    assert _MOCK_SENTIMENT.top_narrative_summary in prompt_text


# ---------------------------------------------------------------------------
# Test 8: Gatekeeper remains terminal validation path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_gatekeeper_remains_terminal_after_sentiment_injection(
    test_config, mock_anthropic_buy_json, mock_polymarket,
):
    """LLMEvaluationResponse.model_validate_json remains the terminal gate.
    Sentiment enrichment must not bypass or alter gatekeeper validation."""
    client, _, out_q = _setup_client(test_config, mock_anthropic_buy_json)

    await client._process_evaluation(_crypto_market_item())

    # Evaluation completed — result must have passed through Gatekeeper
    assert out_q.qsize() == 1
    result = out_q.get_nowait()
    eval_resp = result["evaluation"]

    # Gatekeeper audit must exist (proves LLMEvaluationResponse validated)
    assert eval_resp.gatekeeper_audit is not None
    assert eval_resp.gatekeeper_audit.all_filters_passed is True
    assert eval_resp.decision_boolean is True
