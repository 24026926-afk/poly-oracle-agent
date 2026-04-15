"""
tests/unit/test_market_router.py

Unit tests for ClaudeClient._route_market Layer 0 classification.
"""

import asyncio
from unittest.mock import MagicMock

import pytest

from src.schemas.llm import MarketCategory


def _make_client():
    """Minimal ClaudeClient stub — no real Anthropic client or DB factory."""
    from src.agents.evaluation.claude_client import ClaudeClient

    config = MagicMock()
    config.anthropic_api_key.get_secret_value.return_value = "sk-test"

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "src.agents.evaluation.claude_client.AsyncAnthropic",
            lambda **kw: MagicMock(),
        )
        client = ClaudeClient(
            in_queue=asyncio.Queue(),
            out_queue=asyncio.Queue(),
            config=config,
            db_session_factory=None,
        )
    return client


@pytest.mark.asyncio
async def test_route_crypto_by_condition_id():
    client = _make_client()
    item = {"condition_id": "0xabc_bitcoin_market", "title": ""}
    assert await client._route_market(item) == MarketCategory.CRYPTO


@pytest.mark.asyncio
async def test_route_crypto_by_title():
    client = _make_client()
    item = {"title": "Will ETH hit $5000?"}
    assert await client._route_market(item) == MarketCategory.CRYPTO


@pytest.mark.asyncio
async def test_route_politics_by_title():
    client = _make_client()
    item = {"title": "Will the election result in a runoff?"}
    assert await client._route_market(item) == MarketCategory.POLITICS


@pytest.mark.asyncio
async def test_route_sports_by_title():
    client = _make_client()
    item = {"title": "NBA Finals Game 7 winner"}
    assert await client._route_market(item) == MarketCategory.SPORTS


@pytest.mark.asyncio
async def test_route_general_fallback():
    client = _make_client()
    item = {"title": "Will it rain tomorrow?", "condition_id": "0x123abc"}
    assert await client._route_market(item) == MarketCategory.GENERAL


@pytest.mark.asyncio
async def test_route_priority_crypto_over_politics():
    client = _make_client()
    item = {"title": "crypto election token vote"}
    assert await client._route_market(item) == MarketCategory.CRYPTO
