"""
Unit tests for orchestrator market activation and metadata propagation.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.agents.context.aggregator import DataAggregator
from src.orchestrator import Orchestrator
from src.schemas.market import MarketMetadata


def _patch_heavy_deps() -> dict:
    mock_w3 = MagicMock()
    mock_w3.eth = MagicMock()
    mock_w3.eth.get_transaction_count = AsyncMock(return_value=0)
    return {
        "AsyncWeb3": MagicMock(return_value=mock_w3),
        "AsyncHTTPProvider": MagicMock(),
        "AsyncSessionLocal": MagicMock(),
        "engine": MagicMock(dispose=AsyncMock()),
    }


def _market(**overrides: object) -> MarketMetadata:
    data = {
        "conditionId": "0xactivate001",
        "question": "Will BTC exceed $100k?",
        "category": "Crypto",
        "tags": ["btc", "crypto"],
        "clobTokenIds": ["yes-token-001", "no-token-001"],
        "endDateIso": "2026-12-31T00:00:00+00:00",
    }
    data.update(overrides)
    return MarketMetadata.model_validate(data)


@pytest.mark.asyncio
async def test_activate_market_updates_ws_mapping_and_aggregator_context(test_config):
    with patch.multiple("src.orchestrator", **_patch_heavy_deps()):
        orch = Orchestrator(test_config)

    orch.aggregator = DataAggregator(
        input_queue=asyncio.Queue(),
        output_queue=asyncio.Queue(),
        condition_id="0xold",
    )
    orch.aggregator.best_bid = 0.40
    orch.aggregator.best_ask = 0.60
    orch.aggregator._last_emit_time = 123.0
    orch.aggregator._last_emitted_midpoint = 0.50

    market = _market()
    await orch._activate_market(market)

    assert orch.active_condition_id == "0xactivate001"
    assert set(orch.ws_client._assets_ids) == {"yes-token-001", "no-token-001"}
    # ws_client._token_id_mapping maps tokens → yes_token_id (for snapshot enrichment)
    assert orch.ws_client._token_id_mapping == {
        "yes-token-001": "yes-token-001",
        "no-token-001": "yes-token-001",
        "0xactivate001": "yes-token-001",
    }
    assert orch.ws_client._token_ids_by_condition == {
        "0xactivate001": ("yes-token-001", "no-token-001")
    }
    # orchestrator._condition_by_token maps tokens → condition_id (for routing)
    assert orch._condition_by_token == {
        "yes-token-001": "0xactivate001",
        "no-token-001": "0xactivate001",
        "0xactivate001": "0xactivate001",
    }
    assert orch.aggregator.condition_id == "0xactivate001"
    assert orch.aggregator.best_bid == 0.0
    assert orch.aggregator.best_ask == 0.0
    assert orch.aggregator._last_emit_time == 0.0
    assert orch.aggregator._last_emitted_midpoint is None
    assert orch.aggregator._market_category == "CRYPTO"
    assert orch.aggregator._market_tags == ["btc", "crypto"]
    assert orch.aggregator._market_question == "Will BTC exceed $100k?"
    assert orch.aggregator._yes_token_id == "yes-token-001"


def test_select_rotation_candidate_waits_for_three_spread_failures(test_config):
    with patch.multiple("src.orchestrator", **_patch_heavy_deps()):
        orch = Orchestrator(test_config)

    orch.active_condition_id = "0xactive"
    orch.aggregator = DataAggregator(
        input_queue=asyncio.Queue(),
        output_queue=asyncio.Queue(),
        condition_id="0xactive",
    )
    orch.aggregator.best_bid = 0.001
    orch.aggregator.best_ask = 0.500
    active = _market(conditionId="0xactive")
    alternate = _market(conditionId="0xalternate")

    assert orch._select_rotation_candidate([active, alternate]) is None
    assert orch._select_rotation_candidate([active, alternate]) is None
    assert orch._consecutive_spread_failures == 2


def test_select_rotation_candidate_returns_first_non_active_after_three_failures(
    test_config,
):
    with patch.multiple("src.orchestrator", **_patch_heavy_deps()):
        orch = Orchestrator(test_config)

    orch.active_condition_id = "0xactive"
    orch.aggregator = DataAggregator(
        input_queue=asyncio.Queue(),
        output_queue=asyncio.Queue(),
        condition_id="0xactive",
    )
    orch.aggregator.best_bid = 0.001
    orch.aggregator.best_ask = 0.500
    active = _market(conditionId="0xactive")
    alternate = _market(conditionId="0xalternate")

    assert orch._select_rotation_candidate([active, alternate]) is None
    assert orch._select_rotation_candidate([active, alternate]) is None
    assert orch._select_rotation_candidate([active, alternate]) == alternate
    assert orch._consecutive_spread_failures == 3


def test_select_rotation_candidate_does_not_rotate_single_market(test_config):
    with patch.multiple("src.orchestrator", **_patch_heavy_deps()):
        orch = Orchestrator(test_config)

    orch.active_condition_id = "0xactive"
    orch.aggregator = DataAggregator(
        input_queue=asyncio.Queue(),
        output_queue=asyncio.Queue(),
        condition_id="0xactive",
    )
    orch.aggregator.best_bid = 0.001
    orch.aggregator.best_ask = 0.500
    active = _market(conditionId="0xactive")

    assert orch._select_rotation_candidate([active]) is None
    assert orch._select_rotation_candidate([active]) is None
    assert orch._select_rotation_candidate([active]) is None
    assert orch._consecutive_spread_failures == 3


def test_select_rotation_candidate_resets_counter_when_spread_recovers(test_config):
    with patch.multiple("src.orchestrator", **_patch_heavy_deps()):
        orch = Orchestrator(test_config)

    orch.active_condition_id = "0xactive"
    orch.aggregator = DataAggregator(
        input_queue=asyncio.Queue(),
        output_queue=asyncio.Queue(),
        condition_id="0xactive",
    )
    active = _market(conditionId="0xactive")
    alternate = _market(conditionId="0xalternate")

    orch.aggregator.best_bid = 0.001
    orch.aggregator.best_ask = 0.500
    assert orch._select_rotation_candidate([active, alternate]) is None
    assert orch._consecutive_spread_failures == 1

    orch.aggregator.best_bid = 0.495
    orch.aggregator.best_ask = 0.500
    assert orch._select_rotation_candidate([active, alternate]) is None
    assert orch._consecutive_spread_failures == 0
