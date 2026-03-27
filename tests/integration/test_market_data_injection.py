"""
tests/integration/test_market_data_injection.py

RED-phase integration tests for WI-14 market data injection into ClaudeClient.
Validates: pre-prompt fetch ordering, enriched prompt context,
and conservative failure-path behavior.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.agents.evaluation.claude_client import ClaudeClient
from src.agents.execution.polymarket_client import MarketSnapshot, PolymarketClient
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


def _approved_reflection():
    return _mock_anthropic_response(APPROVED_REFLECTION_JSON)


def _make_market_snapshot() -> MarketSnapshot:
    """Return a valid MarketSnapshot with Decimal fields."""
    from datetime import datetime, timezone

    return MarketSnapshot(
        token_id="tok-yes-001",
        best_bid=Decimal("0.48"),
        best_ask=Decimal("0.52"),
        midpoint_probability=Decimal("0.50"),
        spread=Decimal("0.04"),
        fetched_at_utc=datetime.now(timezone.utc),
        source="clob_orderbook",
    )


def _make_eval_item(*, yes_token_id: str = "tok-yes-001") -> dict:
    """Build a minimal evaluation queue item with yes_token_id."""
    return {
        "snapshot_id": "snap-wi14-001",
        "prompt": "Evaluate this market",
        "yes_token_id": yes_token_id,
        "state": {
            "condition_id": "0xaaaa1111bbbb2222cccc3333dddd4444eeee5555",
            "best_bid": 0.45,
            "best_ask": 0.455,
            "midpoint": 0.4525,
            "spread": 0.005,
            "timestamp": 1700000000,
        },
    }


def _make_buy_response_json() -> str:
    """Build a raw JSON string that passes LLMEvaluationResponse validation."""
    from datetime import datetime, timedelta, timezone

    end_date = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    payload = {
        "market_context": {
            "condition_id": "0xaaaa1111bbbb2222cccc3333dddd4444eeee5555",
            "outcome_evaluated": "YES",
            "best_bid": 0.48,
            "best_ask": 0.52,
            "midpoint": 0.50,
            "market_end_date": end_date,
        },
        "probabilistic_estimate": {
            "p_true": 0.65,
            "p_market": 0.50,
        },
        "risk_assessment": {
            "liquidity_risk_score": 0.2,
            "resolution_risk_score": 0.1,
            "information_asymmetry_flag": False,
            "risk_notes": "Low risk market.",
        },
        "confidence_score": 0.85,
        "decision_boolean": True,
        "recommended_action": "BUY",
        "reasoning_log": "Positive EV detected based on fresh market data.",
    }
    return json.dumps(payload)


# ---------------------------------------------------------------------------
# 1. ClaudeClient fetches WI-14 market data BEFORE prompt construction
# ---------------------------------------------------------------------------


class TestPrePromptFetchOrdering:
    """ClaudeClient must call PolymarketClient.fetch_order_book before
    PromptFactory.build_evaluation_prompt."""

    @pytest.mark.asyncio
    async def test_fetch_order_book_called_before_prompt_build(self, test_config):
        """fetch_order_book is invoked before build_evaluation_prompt."""
        in_q: asyncio.Queue = asyncio.Queue()
        out_q: asyncio.Queue = asyncio.Queue()
        client = ClaudeClient(in_queue=in_q, out_queue=out_q, config=test_config)

        call_order: list[str] = []

        # Track call ordering
        snapshot = _make_market_snapshot()

        async def mock_fetch_order_book(token_id: str):
            call_order.append("fetch_order_book")
            return snapshot

        original_build = None

        with patch(
            "src.agents.evaluation.claude_client.PolymarketClient"
        ) as MockPMClient:
            mock_pm_instance = MagicMock()
            mock_pm_instance.fetch_order_book = AsyncMock(
                side_effect=mock_fetch_order_book
            )
            MockPMClient.return_value = mock_pm_instance

            with patch(
                "src.agents.evaluation.claude_client.PromptFactory"
            ) as MockPF:
                def track_build(*args, **kwargs):
                    call_order.append("build_evaluation_prompt")
                    return "mocked prompt"

                MockPF.build_evaluation_prompt = MagicMock(side_effect=track_build)
                MockPF.build_reflection_prompt = MagicMock(return_value="mocked reflection prompt")

                buy_json = _make_buy_response_json()
                client.client = MagicMock()
                client.client.messages.create = AsyncMock(
                    side_effect=[
                        _mock_anthropic_response(buy_json),
                        _approved_reflection(),
                    ],
                )
                client._persist_decision = AsyncMock()

                await client._process_evaluation(_make_eval_item())

        assert "fetch_order_book" in call_order
        assert "build_evaluation_prompt" in call_order
        assert call_order.index("fetch_order_book") < call_order.index(
            "build_evaluation_prompt"
        ), "fetch_order_book must be called BEFORE build_evaluation_prompt"


# ---------------------------------------------------------------------------
# 2. Prompt context reflects refreshed WI-14 spread/midpoint values
# ---------------------------------------------------------------------------


class TestPromptContextEnrichment:
    """PromptFactory must receive enriched market_state from WI-14 snapshot."""

    @pytest.mark.asyncio
    async def test_prompt_receives_refreshed_bid_ask(self, test_config):
        """build_evaluation_prompt receives updated best_bid/best_ask from snapshot."""
        in_q: asyncio.Queue = asyncio.Queue()
        out_q: asyncio.Queue = asyncio.Queue()
        client = ClaudeClient(in_queue=in_q, out_queue=out_q, config=test_config)

        snapshot = _make_market_snapshot()
        captured_market_state = {}

        with patch(
            "src.agents.evaluation.claude_client.PolymarketClient"
        ) as MockPMClient:
            mock_pm_instance = MagicMock()
            mock_pm_instance.fetch_order_book = AsyncMock(return_value=snapshot)
            MockPMClient.return_value = mock_pm_instance

            with patch(
                "src.agents.evaluation.claude_client.PromptFactory"
            ) as MockPF:
                def capture_build(market_state, **kwargs):
                    captured_market_state.update(market_state)
                    return "mocked prompt"

                MockPF.build_evaluation_prompt = MagicMock(side_effect=capture_build)
                MockPF.build_reflection_prompt = MagicMock(return_value="mocked reflection prompt")

                buy_json = _make_buy_response_json()
                client.client = MagicMock()
                client.client.messages.create = AsyncMock(
                    side_effect=[
                        _mock_anthropic_response(buy_json),
                        _approved_reflection(),
                    ],
                )
                client._persist_decision = AsyncMock()

                await client._process_evaluation(_make_eval_item())

        # The market state passed to PromptFactory should reflect WI-14 snapshot values
        assert Decimal(str(captured_market_state["best_bid"])) == Decimal("0.48")
        assert Decimal(str(captured_market_state["best_ask"])) == Decimal("0.52")
        assert Decimal(str(captured_market_state["midpoint"])) == Decimal("0.50")

    @pytest.mark.asyncio
    async def test_prompt_receives_spread_from_snapshot(self, test_config):
        """build_evaluation_prompt receives spread value from WI-14 snapshot."""
        in_q: asyncio.Queue = asyncio.Queue()
        out_q: asyncio.Queue = asyncio.Queue()
        client = ClaudeClient(in_queue=in_q, out_queue=out_q, config=test_config)

        snapshot = _make_market_snapshot()
        captured_market_state = {}

        with patch(
            "src.agents.evaluation.claude_client.PolymarketClient"
        ) as MockPMClient:
            mock_pm_instance = MagicMock()
            mock_pm_instance.fetch_order_book = AsyncMock(return_value=snapshot)
            MockPMClient.return_value = mock_pm_instance

            with patch(
                "src.agents.evaluation.claude_client.PromptFactory"
            ) as MockPF:
                def capture_build(market_state, **kwargs):
                    captured_market_state.update(market_state)
                    return "mocked prompt"

                MockPF.build_evaluation_prompt = MagicMock(side_effect=capture_build)
                MockPF.build_reflection_prompt = MagicMock(return_value="mocked reflection prompt")

                buy_json = _make_buy_response_json()
                client.client = MagicMock()
                client.client.messages.create = AsyncMock(
                    side_effect=[
                        _mock_anthropic_response(buy_json),
                        _approved_reflection(),
                    ],
                )
                client._persist_decision = AsyncMock()

                await client._process_evaluation(_make_eval_item())

        assert Decimal(str(captured_market_state["spread"])) == Decimal("0.04")


# ---------------------------------------------------------------------------
# 3. Market-data failure path → conservative behavior (no execution enqueue)
# ---------------------------------------------------------------------------


class TestMarketDataFailurePath:
    """When WI-14 market data fetch fails, no execution-eligible output."""

    @pytest.mark.asyncio
    async def test_fetch_failure_blocks_execution_enqueue(self, test_config):
        """If fetch_order_book returns None, nothing reaches execution queue."""
        in_q: asyncio.Queue = asyncio.Queue()
        out_q: asyncio.Queue = asyncio.Queue()
        client = ClaudeClient(in_queue=in_q, out_queue=out_q, config=test_config)

        with patch(
            "src.agents.evaluation.claude_client.PolymarketClient"
        ) as MockPMClient:
            mock_pm_instance = MagicMock()
            mock_pm_instance.fetch_order_book = AsyncMock(return_value=None)
            MockPMClient.return_value = mock_pm_instance

            client.client = MagicMock()
            client.client.messages.create = AsyncMock()
            client._persist_decision = AsyncMock()

            await client._process_evaluation(_make_eval_item())

        # No item should reach execution queue on market data failure
        assert out_q.qsize() == 0
        # Claude should NOT even be called if market data is unavailable
        assert client.client.messages.create.call_count == 0

    @pytest.mark.asyncio
    async def test_fetch_timeout_blocks_execution_enqueue(self, test_config):
        """If fetch_order_book times out, nothing reaches execution queue."""
        in_q: asyncio.Queue = asyncio.Queue()
        out_q: asyncio.Queue = asyncio.Queue()
        client = ClaudeClient(in_queue=in_q, out_queue=out_q, config=test_config)

        with patch(
            "src.agents.evaluation.claude_client.PolymarketClient"
        ) as MockPMClient:
            mock_pm_instance = MagicMock()
            mock_pm_instance.fetch_order_book = AsyncMock(
                side_effect=asyncio.TimeoutError()
            )
            MockPMClient.return_value = mock_pm_instance

            client.client = MagicMock()
            client.client.messages.create = AsyncMock()
            client._persist_decision = AsyncMock()

            await client._process_evaluation(_make_eval_item())

        assert out_q.qsize() == 0

    @pytest.mark.asyncio
    async def test_missing_yes_token_id_blocks_execution(self, test_config):
        """If yes_token_id is missing from item, conservative non-trading behavior."""
        in_q: asyncio.Queue = asyncio.Queue()
        out_q: asyncio.Queue = asyncio.Queue()
        client = ClaudeClient(in_queue=in_q, out_queue=out_q, config=test_config)

        # Item without yes_token_id
        item = {
            "snapshot_id": "snap-no-token-001",
            "prompt": "Evaluate this market",
            "state": {
                "condition_id": "0xaaaa1111bbbb2222cccc3333dddd4444eeee5555",
                "best_bid": 0.45,
                "best_ask": 0.455,
                "midpoint": 0.4525,
                "spread": 0.005,
                "timestamp": 1700000000,
            },
        }

        with patch(
            "src.agents.evaluation.claude_client.PolymarketClient"
        ) as MockPMClient:
            mock_pm_instance = MagicMock()
            mock_pm_instance.fetch_order_book = AsyncMock()
            MockPMClient.return_value = mock_pm_instance

            client.client = MagicMock()
            client.client.messages.create = AsyncMock()
            client._persist_decision = AsyncMock()

            await client._process_evaluation(item)

        # No execution enqueue when yes_token_id is absent
        assert out_q.qsize() == 0
        # fetch_order_book should NOT have been called without a token_id
        assert mock_pm_instance.fetch_order_book.call_count == 0
