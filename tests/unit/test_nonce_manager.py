"""
tests/unit/test_nonce_manager.py

Async unit tests for the NonceManager concurrency guard.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.agents.execution.nonce_manager import NonceManager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
MOCK_ADDRESS = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
INITIAL_NONCE = 42


def _mock_w3(nonce: int = INITIAL_NONCE) -> MagicMock:
    """Build an AsyncWeb3 mock whose eth.get_transaction_count returns *nonce*."""
    w3 = MagicMock()
    w3.eth = MagicMock()
    w3.eth.get_transaction_count = AsyncMock(return_value=nonce)
    return w3


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_initialize_fetches_nonce_from_rpc():
    w3 = _mock_w3(INITIAL_NONCE)
    mgr = NonceManager(w3, MOCK_ADDRESS)

    await mgr.initialize()

    assert mgr.current_nonce == INITIAL_NONCE


@pytest.mark.asyncio
async def test_get_next_nonce_returns_then_increments():
    w3 = _mock_w3(INITIAL_NONCE)
    mgr = NonceManager(w3, MOCK_ADDRESS)
    await mgr.initialize()

    first = await mgr.get_next_nonce()
    second = await mgr.get_next_nonce()

    assert first == INITIAL_NONCE
    assert second == INITIAL_NONCE + 1


@pytest.mark.asyncio
async def test_get_next_nonce_before_initialize_raises():
    w3 = _mock_w3()
    mgr = NonceManager(w3, MOCK_ADDRESS)

    with pytest.raises(RuntimeError, match="not initialized"):
        await mgr.get_next_nonce()


@pytest.mark.asyncio
async def test_sync_updates_nonce_from_chain():
    w3 = _mock_w3(INITIAL_NONCE)
    mgr = NonceManager(w3, MOCK_ADDRESS)
    await mgr.initialize()

    # Consume a few nonces locally
    await mgr.get_next_nonce()
    await mgr.get_next_nonce()
    assert mgr.current_nonce == INITIAL_NONCE + 2

    # Chain reports a different value after sync
    new_chain_nonce = 100
    w3.eth.get_transaction_count = AsyncMock(return_value=new_chain_nonce)

    await mgr.sync()

    assert mgr.current_nonce == new_chain_nonce


@pytest.mark.asyncio
async def test_concurrent_nonces_are_unique():
    w3 = _mock_w3(0)
    mgr = NonceManager(w3, MOCK_ADDRESS)
    await mgr.initialize()

    results = await asyncio.gather(
        *(mgr.get_next_nonce() for _ in range(10))
    )

    # All 10 must be unique and form a contiguous range 0..9
    assert sorted(results) == list(range(10))


@pytest.mark.asyncio
async def test_sync_logs_old_and_new_nonce():
    w3 = _mock_w3(INITIAL_NONCE)
    mgr = NonceManager(w3, MOCK_ADDRESS)
    await mgr.initialize()

    new_chain_nonce = 99
    w3.eth.get_transaction_count = AsyncMock(return_value=new_chain_nonce)

    with patch("src.agents.execution.nonce_manager.logger") as mock_logger:
        await mgr.sync()

        mock_logger.info.assert_called_once_with(
            "nonce_manager.synced",
            old_nonce=INITIAL_NONCE,
            new_nonce=new_chain_nonce,
        )


@pytest.mark.asyncio
async def test_dry_run_does_not_advance_nonce():
    """When dry_run=True, get_next_nonce returns sentinel -1 and nonce stays put."""
    w3 = _mock_w3(INITIAL_NONCE)
    mgr = NonceManager(w3, MOCK_ADDRESS, dry_run=True)
    await mgr.initialize()

    result = await mgr.get_next_nonce()

    assert result == -1
    # Internal nonce must not have changed (dry-run initialize sets to 0)
    assert mgr.current_nonce == 0


@pytest.mark.asyncio
async def test_initialize_uses_pending_block_tag():
    w3 = _mock_w3(INITIAL_NONCE)
    mgr = NonceManager(w3, MOCK_ADDRESS)

    await mgr.initialize()

    w3.eth.get_transaction_count.assert_called_once()
    call_args = w3.eth.get_transaction_count.call_args
    assert call_args[0][1] == "pending"


# ---------------------------------------------------------------------------
# Dry-run short-circuit — no RPC calls at all
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_dry_run_initialize_skips_rpc_and_sets_nonce_zero():
    """When dry_run=True, initialize() must NOT call get_transaction_count
    and must set nonce to 0."""
    w3 = _mock_w3(INITIAL_NONCE)
    mgr = NonceManager(w3, MOCK_ADDRESS, dry_run=True)

    await mgr.initialize()

    w3.eth.get_transaction_count.assert_not_called()
    assert mgr.current_nonce == 0


@pytest.mark.asyncio
async def test_dry_run_sync_skips_rpc():
    """When dry_run=True, sync() must NOT call get_transaction_count."""
    w3 = _mock_w3(INITIAL_NONCE)
    mgr = NonceManager(w3, MOCK_ADDRESS, dry_run=True)
    await mgr.initialize()

    await mgr.sync()

    w3.eth.get_transaction_count.assert_not_called()
    # Nonce should remain 0
    assert mgr.current_nonce == 0
