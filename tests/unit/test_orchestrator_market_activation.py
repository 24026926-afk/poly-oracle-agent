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


# ── F3 (2026-05-23) orchestrator.market_activated dedup ─────────────────────


def _activation_event_names(mock_info) -> list[str]:
    """Extract the first positional arg (event name) from each logger.info call."""
    return [
        call.args[0] if call.args else call.kwargs.get("event", "")
        for call in mock_info.call_args_list
    ]


@pytest.mark.asyncio
async def test_activate_markets_emits_info_on_first_activation(test_config):
    """First activation of a market must emit exactly one market_activated INFO."""
    with patch.multiple("src.orchestrator", **_patch_heavy_deps()):
        orch = Orchestrator(test_config)

    market = _market(conditionId="0xnewcid")
    with patch("src.orchestrator.logger") as mock_logger:
        await orch._activate_markets([market])
        events = _activation_event_names(mock_logger.info)

    assert events.count("orchestrator.market_activated") == 1
    assert orch._last_activated_condition_ids == frozenset({"0xnewcid"})


@pytest.mark.asyncio
async def test_activate_markets_unchanged_set_emits_no_info(test_config):
    """Re-calling with the same activated set must emit no INFO market_activated."""
    with patch.multiple("src.orchestrator", **_patch_heavy_deps()):
        orch = Orchestrator(test_config)

    market = _market(conditionId="0xstable")
    await orch._activate_markets([market])  # seed first

    with patch("src.orchestrator.logger") as mock_logger:
        await orch._activate_markets([market])  # same set
        events = _activation_event_names(mock_logger.info)

    assert "orchestrator.market_activated" not in events
    assert "orchestrator.market_deactivated" not in events
    # DEBUG heartbeat must still fire
    debug_events = _activation_event_names(mock_logger.debug)
    assert "orchestrator.market_activation_unchanged" in debug_events


@pytest.mark.asyncio
async def test_activate_markets_diff_emits_info_for_added_and_deactivated_for_removed(
    test_config,
):
    """Adding one market emits one INFO line for the new cid; removing one
    emits one INFO market_deactivated for the dropped cid."""
    with patch.multiple("src.orchestrator", **_patch_heavy_deps()):
        orch = Orchestrator(test_config)

    market_a = _market(conditionId="0xcid_a", clobTokenIds=["yes-a", "no-a"])
    market_b = _market(conditionId="0xcid_b", clobTokenIds=["yes-b", "no-b"])
    market_c = _market(conditionId="0xcid_c", clobTokenIds=["yes-c", "no-c"])

    await orch._activate_markets([market_a, market_b])  # seed

    with patch("src.orchestrator.logger") as mock_logger:
        await orch._activate_markets([market_a, market_c])  # add c, remove b
        events = _activation_event_names(mock_logger.info)

    assert events.count("orchestrator.market_activated") == 1  # only c is new
    assert events.count("orchestrator.market_deactivated") == 1  # only b is gone
    assert orch._last_activated_condition_ids == frozenset({"0xcid_a", "0xcid_c"})
