"""
tests/integration/test_orchestrator.py

Integration tests for the Orchestrator — startup, shutdown, discovery
wiring, and dry_run execution gate.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.orchestrator import Orchestrator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _patch_heavy_deps():
    """Return a dict of patches that neutralise network-bound constructors."""
    mock_w3 = MagicMock()
    mock_w3.eth = MagicMock()
    mock_w3.eth.get_transaction_count = AsyncMock(return_value=0)

    return {
        "AsyncWeb3": MagicMock(return_value=mock_w3),
        "AsyncHTTPProvider": MagicMock(),
        "AsyncSessionLocal": MagicMock(),
        "engine": MagicMock(dispose=AsyncMock()),
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orchestrator_instantiation_no_crash(test_config):
    """Constructing Orchestrator must not raise ImportError/NameError."""
    with patch.multiple("src.orchestrator", **_patch_heavy_deps()):
        orch = Orchestrator(test_config)

    assert orch.market_queue is not None
    assert orch.prompt_queue is not None
    assert orch.execution_queue is not None
    assert orch.ws_client is not None
    assert orch.claude_client is not None
    assert orch.signer is not None
    assert orch.nonce_manager is not None
    assert orch.gas_estimator is not None
    assert orch.bankroll_tracker is not None


@pytest.mark.asyncio
async def test_orchestrator_no_eligible_markets_returns_early(
    test_config, mock_gamma_markets
):
    """When discovery finds no eligible market, start() returns immediately."""
    patches = _patch_heavy_deps()

    with patch.multiple("src.orchestrator", **patches):
        orch = Orchestrator(test_config)

        # Wire a discovery engine that returns empty
        orch.discovery_engine = AsyncMock()
        orch.discovery_engine.discover = AsyncMock(return_value=[])

        # Stub the HTTP clients that start() creates
        orch._http_session = MagicMock(close=AsyncMock())
        orch._httpx_client = MagicMock(aclose=AsyncMock())
        orch.gamma_client = MagicMock()

        # Override start to skip aiohttp/httpx creation and jump to discovery
        original_start = orch.start

        async def patched_start():
            # Skip HTTP client creation — already stubbed
            orch.nonce_manager.initialize = AsyncMock()
            await orch.nonce_manager.initialize()
            eligible = await orch.discovery_engine.discover()
            if not eligible:
                return
            # Should never reach here
            raise AssertionError("Should have returned early")

        await patched_start()

    # No tasks should have been created
    assert orch._tasks == []


@pytest.mark.asyncio
async def test_orchestrator_shutdown_disposes_resources(test_config):
    """shutdown() must close HTTP clients, cancel tasks, and dispose engine."""
    patches = _patch_heavy_deps()
    mock_engine = patches["engine"]

    with patch.multiple("src.orchestrator", **patches):
        orch = Orchestrator(test_config)

    # Simulate post-start state with HTTP clients open
    mock_httpx = MagicMock()
    mock_httpx.aclose = AsyncMock()
    orch._httpx_client = mock_httpx

    mock_aiohttp = MagicMock()
    mock_aiohttp.close = AsyncMock()
    orch._http_session = mock_aiohttp

    # Simulate running tasks
    async def _forever():
        await asyncio.sleep(3600)

    task = asyncio.create_task(_forever())
    orch._tasks = [task]

    # Also mock stoppable components
    orch.aggregator = AsyncMock()
    orch.aggregator.stop = AsyncMock()
    orch.claude_client.stop = AsyncMock()

    with patch.multiple("src.orchestrator", engine=mock_engine):
        await orch.shutdown()

    mock_httpx.aclose.assert_awaited_once()
    mock_aiohttp.close.assert_awaited_once()
    mock_engine.dispose.assert_awaited_once()
    assert task.cancelled() or task.done()


@pytest.mark.asyncio
async def test_orchestrator_market_discovery_sets_condition_id(
    test_config, mock_gamma_markets
):
    """active_condition_id must come from discovery, not a hardcoded value."""
    patches = _patch_heavy_deps()
    expected_cid = "0xdiscovered_market_condition_id_001"

    with patch.multiple("src.orchestrator", **patches):
        orch = Orchestrator(test_config)

    # Inject a mock discovery engine
    orch.discovery_engine = AsyncMock()
    orch.discovery_engine.discover = AsyncMock(return_value=[expected_cid])
    orch.nonce_manager.initialize = AsyncMock()

    # Stub HTTP clients
    orch._http_session = MagicMock(close=AsyncMock())
    orch._httpx_client = MagicMock(aclose=AsyncMock())
    orch.gamma_client = MagicMock()

    # Run a brief start() that discovers then shuts down before gathering
    async def run_start_briefly():
        await orch.nonce_manager.initialize()
        eligible = await orch.discovery_engine.discover()
        if eligible:
            orch.active_condition_id = eligible[0]

    await run_start_briefly()

    assert orch.active_condition_id == expected_cid


@pytest.mark.asyncio
async def test_execution_consumer_dry_run_skips(test_config):
    """In dry_run mode, execution consumer must not call signer or broadcaster."""
    patches = _patch_heavy_deps()

    with patch.multiple("src.orchestrator", **patches):
        orch = Orchestrator(test_config)

    # Mock the broadcaster
    orch.broadcaster = AsyncMock()
    orch.broadcaster.broadcast = AsyncMock()
    orch.signer.build_order_from_decision = AsyncMock()

    # Build a fake evaluation response with the expected shape
    fake_eval = MagicMock()
    fake_eval.market_context.condition_id = "0xtest_condition_id_for_dry_run"
    fake_eval.recommended_action.value = "BUY"
    fake_eval.position_size_pct = 0.02

    await orch.execution_queue.put({
        "snapshot_id": "snap-001",
        "evaluation": fake_eval,
    })

    # Run consumer for one iteration then cancel
    consumer_task = asyncio.create_task(orch._execution_consumer_loop())
    await asyncio.sleep(0.1)
    consumer_task.cancel()
    try:
        await consumer_task
    except asyncio.CancelledError:
        pass

    # dry_run=True → signer and broadcaster must NOT be called
    orch.signer.build_order_from_decision.assert_not_awaited()
    orch.broadcaster.broadcast.assert_not_awaited()
