"""
tests/unit/test_broadcaster.py

Async unit tests for the OrderBroadcaster lifecycle.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.agents.execution.broadcaster import OrderBroadcaster
from src.core.exceptions import BroadcastError
from src.schemas.web3 import (
    GasPrice,
    OrderData,
    OrderSide,
    SignedOrder,
    TxReceiptSchema,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _gas_price() -> GasPrice:
    return GasPrice(
        base_fee_wei=30_000_000_000,
        priority_fee_wei=2_000_000_000,
        max_fee_per_gas_wei=62_000_000_000,
        max_fee_per_gas_gwei=62.0,
    )


def _signed_order() -> SignedOrder:
    order = OrderData(
        salt=999,
        maker="0xABCD",
        signer="0xABCD",
        taker="0x0000000000000000000000000000000000000000",
        token_id=123456,
        maker_amount=50_000_000,
        taker_amount=100_000_000,
        side=OrderSide.BUY,
    )
    return SignedOrder(order=order, signature="0xdeadbeef", owner="0xABCD")


def _receipt_dict(status: int = 1) -> dict:
    return {
        "status": status,
        "blockNumber": 50_000_000,
        "gasUsed": 21_000,
        "transactionHash": "0xabc123",
    }


def _mock_gas_estimator(gas: GasPrice | None = None) -> MagicMock:
    est = MagicMock()
    est.estimate = AsyncMock(return_value=gas or _gas_price())
    return est


def _mock_nonce_manager(nonce: int = 42) -> MagicMock:
    mgr = MagicMock()
    mgr.get_next_nonce = AsyncMock(return_value=nonce)
    mgr.sync = AsyncMock()
    return mgr


def _mock_db_factory() -> MagicMock:
    """Return an async_sessionmaker mock whose session tracks .add() calls."""
    session = MagicMock()
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.close = AsyncMock()

    # async context manager protocol
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    factory = MagicMock(return_value=session)
    factory._last_session = session  # test helper
    return factory


class _FakeResponse:
    """Minimal aiohttp response mock supporting async context manager."""

    def __init__(self, status: int, body: dict | str) -> None:
        self.status = status
        self._body = body

    async def text(self) -> str:
        if isinstance(self._body, dict):
            return json.dumps(self._body)
        return self._body

    async def json(self) -> dict:
        if isinstance(self._body, dict):
            return self._body
        return json.loads(self._body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


def _mock_http_session(status: int = 200, body: dict | None = None) -> MagicMock:
    resp_body = body or {"orderID": "order-abc-123"}
    session = MagicMock()
    session.post = MagicMock(return_value=_FakeResponse(status, resp_body))
    return session


def _mock_w3(receipt: dict | None = None) -> MagicMock:
    w3 = MagicMock()
    w3.eth = MagicMock()
    w3.eth.get_transaction_receipt = AsyncMock(return_value=receipt)
    return w3


def _build_broadcaster(
    *,
    w3: MagicMock | None = None,
    nonce_mgr: MagicMock | None = None,
    gas_est: MagicMock | None = None,
    http: MagicMock | None = None,
    db: MagicMock | None = None,
    poll_max_attempts: int = 2,
    poll_delay_s: float = 0.0,
) -> OrderBroadcaster:
    return OrderBroadcaster(
        w3=w3 or _mock_w3(_receipt_dict()),
        nonce_manager=nonce_mgr or _mock_nonce_manager(),
        gas_estimator=gas_est or _mock_gas_estimator(),
        http_session=http or _mock_http_session(),
        db_session_factory=db or _mock_db_factory(),
        clob_rest_url="https://clob.polymarket.com",
        poll_max_attempts=poll_max_attempts,
        poll_delay_s=poll_delay_s,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_broadcast_happy_path():
    w3 = _mock_w3(_receipt_dict())
    bc = _build_broadcaster(w3=w3)

    result = await bc.broadcast(_signed_order(), decision_id="dec-1")

    assert isinstance(result, TxReceiptSchema)
    assert result.status == "CONFIRMED"
    assert result.gas_used == 21_000
    assert result.block_number == 50_000_000


@pytest.mark.asyncio
async def test_broadcast_persists_execution_tx():
    db = _mock_db_factory()
    w3 = _mock_w3(_receipt_dict())
    bc = _build_broadcaster(w3=w3, db=db)

    await bc.broadcast(_signed_order(), decision_id="dec-1")

    session = db._last_session
    session.add.assert_called_once()
    row = session.add.call_args[0][0]
    assert row.decision_id == "dec-1"
    assert row.nonce == 42
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_broadcast_4xx_raises_and_syncs_nonce():
    http = _mock_http_session(status=400, body={"error": "bad request"})
    nonce_mgr = _mock_nonce_manager()
    bc = _build_broadcaster(http=http, nonce_mgr=nonce_mgr)

    with pytest.raises(BroadcastError) as exc_info:
        await bc.broadcast(_signed_order(), decision_id="dec-1")

    assert exc_info.value.status_code == 400
    nonce_mgr.sync.assert_awaited_once()


@pytest.mark.asyncio
async def test_broadcast_5xx_raises_no_nonce_sync():
    http = _mock_http_session(status=500, body={"error": "internal"})
    nonce_mgr = _mock_nonce_manager()
    bc = _build_broadcaster(http=http, nonce_mgr=nonce_mgr)

    with pytest.raises(BroadcastError) as exc_info:
        await bc.broadcast(_signed_order(), decision_id="dec-1")

    assert exc_info.value.status_code == 500
    nonce_mgr.sync.assert_not_awaited()


@pytest.mark.asyncio
async def test_poll_receipt_retries_until_found():
    w3 = _mock_w3()
    # None → None → None → receipt on 4th call
    w3.eth.get_transaction_receipt = AsyncMock(
        side_effect=[None, None, None, _receipt_dict()]
    )
    bc = _build_broadcaster(w3=w3)

    # Call _poll_receipt directly with tiny delay
    result = await bc._poll_receipt("order-x", max_attempts=5, delay_s=0.0)

    assert result.status == "CONFIRMED"
    assert w3.eth.get_transaction_receipt.call_count == 4


@pytest.mark.asyncio
async def test_poll_receipt_timeout_raises():
    w3 = _mock_w3(receipt=None)  # always None
    bc = _build_broadcaster(w3=w3)

    with pytest.raises(BroadcastError, match="timeout"):
        await bc._poll_receipt("order-x", max_attempts=2, delay_s=0.0)


@pytest.mark.asyncio
async def test_poll_receipt_timeout_still_persists_db():
    w3 = _mock_w3(receipt=None)
    db = _mock_db_factory()
    bc = _build_broadcaster(w3=w3, db=db)

    with pytest.raises(BroadcastError):
        await bc.broadcast(_signed_order(), decision_id="dec-timeout")

    session = db._last_session
    session.add.assert_called_once()
    row = session.add.call_args[0][0]
    assert row.decision_id == "dec-timeout"
    assert row.status.value == "PENDING"
    assert row.error_message is not None


@pytest.mark.asyncio
async def test_dry_run_prevents_all_execution():
    """When dry_run=True, broadcast() must not call gas, nonce, HTTP, or DB."""
    config = MagicMock()
    config.dry_run = True

    gas_est = _mock_gas_estimator()
    nonce_mgr = _mock_nonce_manager()
    http = _mock_http_session()
    db = _mock_db_factory()

    bc = OrderBroadcaster(
        w3=_mock_w3(_receipt_dict()),
        nonce_manager=nonce_mgr,
        gas_estimator=gas_est,
        http_session=http,
        db_session_factory=db,
        clob_rest_url="https://clob.polymarket.com",
        config=config,
    )

    result = await bc.broadcast(_signed_order(), decision_id="dec-dry")

    assert isinstance(result, TxReceiptSchema)
    assert result.status == "DRY_RUN"
    assert result.order_id == "dry-run"

    # Zero side effects
    gas_est.estimate.assert_not_awaited()
    nonce_mgr.get_next_nonce.assert_not_awaited()
    http.post.assert_not_called()
    db._last_session.add.assert_not_called()


@pytest.mark.asyncio
async def test_gas_price_logged_on_submission():
    w3 = _mock_w3(_receipt_dict())
    bc = _build_broadcaster(w3=w3)

    with patch("src.agents.execution.broadcaster.logger") as mock_logger:
        await bc.broadcast(_signed_order(), decision_id="dec-1")

        # Find the order_submitted log call
        calls = [c for c in mock_logger.info.call_args_list
                 if c[0][0] == "broadcaster.order_submitted"]
        assert len(calls) == 1
        assert calls[0][1]["gas_gwei"] == 62.0
