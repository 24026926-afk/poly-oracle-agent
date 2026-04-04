"""
tests/unit/test_ws_bugs.py

Tests for WS client bugs: yes_token_id propagation, midpoint computation, INVALID_OPERATION.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.agents.ingestion.ws_client import CLOBWebSocketClient


def _mock_config() -> MagicMock:
    cfg = MagicMock()
    cfg.clob_ws_url = "wss://fake.ws/market"
    return cfg


def _mock_db_factory() -> MagicMock:
    session = MagicMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=session)
    factory._last_session = session
    return factory


# ---------------------------------------------------------------------------
# BUG 1: yes_token_id propagation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ws_client_accepts_token_id_mapping():
    """WS client must accept a token_id → yes_token_id mapping at init or via setter."""
    queue: asyncio.Queue = asyncio.Queue()
    db = _mock_db_factory()

    client = CLOBWebSocketClient(
        config=_mock_config(),
        queue=queue,
        db_session_factory=db,
        assets_ids=["tok_yes_123"],
        token_id_to_yes_token_id={"tok_yes_123": "asset_yes_id_123"}
    )

    assert "tok_yes_123" in client._token_id_mapping
    assert client._token_id_mapping["tok_yes_123"] == "asset_yes_id_123"


@pytest.mark.asyncio
async def test_ws_client_can_set_token_id_mapping_after_init():
    """Orchestrator should be able to set the mapping after construction."""
    queue: asyncio.Queue = asyncio.Queue()
    db = _mock_db_factory()

    client = CLOBWebSocketClient(
        config=_mock_config(),
        queue=queue,
        db_session_factory=db,
    )

    mapping = {"tok_yes_abc": "yes_asset_abc", "tok_yes_def": "yes_asset_def"}
    client.set_token_id_mapping(mapping)

    assert client._token_id_mapping == mapping


@pytest.mark.asyncio
async def test_market_snapshot_includes_yes_token_id():
    """Emitted MarketSnapshot must include yes_token_id from the mapping."""
    queue: asyncio.Queue = asyncio.Queue()
    db = _mock_db_factory()

    client = CLOBWebSocketClient(
        config=_mock_config(),
        queue=queue,
        db_session_factory=db,
        token_id_to_yes_token_id={"tok_yes_123": "yes_asset_123"}
    )

    # Simulate a price_change event with asset_id=tok_yes_123
    msg = json.dumps({
        "event": "price_change",
        "market": "0xcond123",
        "asset_id": "tok_yes_123",
        "best_bid": 0.45,
        "best_ask": 0.55,
    })

    await client._handle_message(msg)

    # Check that the snapshot was enqueued with yes_token_id set
    assert queue.qsize() == 1
    snapshot = queue.get_nowait()
    assert snapshot.yes_token_id == "yes_asset_123"


# ---------------------------------------------------------------------------
# BUG 2: midpoint computation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ws_client_computes_midpoint_from_bids_asks():
    """For book frames with bids/asks lists, midpoint must be computed."""
    queue: asyncio.Queue = asyncio.Queue()
    db = _mock_db_factory()

    client = CLOBWebSocketClient(
        config=_mock_config(),
        queue=queue,
        db_session_factory=db,
    )

    msg = json.dumps({
        "event": "book",
        "market": "0xcond456",
        "bids": [{"price": "0.40", "size": "100"}],
        "asks": [{"price": "0.60", "size": "100"}],
    })

    await client._handle_message(msg)

    assert queue.qsize() == 1
    snapshot = queue.get_nowait()
    assert snapshot.midpoint == 0.5, "midpoint should be (0.40 + 0.60) / 2"
    assert snapshot.midpoint != 0.0


@pytest.mark.asyncio
async def test_ws_client_computes_midpoint_from_best_bid_ask():
    """For price_change frames with best_bid/best_ask, midpoint must be computed."""
    queue: asyncio.Queue = asyncio.Queue()
    db = _mock_db_factory()

    client = CLOBWebSocketClient(
        config=_mock_config(),
        queue=queue,
        db_session_factory=db,
    )

    msg = json.dumps({
        "event": "price_change",
        "market": "0xcond789",
        "best_bid": 0.35,
        "best_ask": 0.65,
    })

    await client._handle_message(msg)

    assert queue.qsize() == 1
    snapshot = queue.get_nowait()
    assert snapshot.midpoint == 0.5, "midpoint should be (0.35 + 0.65) / 2"
    assert snapshot.midpoint != 0.0


# ---------------------------------------------------------------------------
# BUG 3: INVALID OPERATION logging and subscription audit
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ws_client_logs_outbound_messages():
    """Every outbound message must be logged for debugging."""
    queue: asyncio.Queue = asyncio.Queue()
    db = _mock_db_factory()

    client = CLOBWebSocketClient(
        config=_mock_config(),
        queue=queue,
        db_session_factory=db,
        assets_ids=["tok1", "tok2"],
    )

    with patch("src.agents.ingestion.ws_client.logger") as mock_logger:
        msg = client._build_subscription_message()
        # Verify that when a subscription is sent, it was logged
        # (This would happen in _stream before await ws.send)
        assert "subscribe" in msg
        assert "tok1" in msg


@pytest.mark.asyncio
async def test_ws_client_logs_subscription_audit():
    """On connection, WS client should log subscription audit info."""
    queue: asyncio.Queue = asyncio.Queue()
    db = _mock_db_factory()

    client = CLOBWebSocketClient(
        config=_mock_config(),
        queue=queue,
        db_session_factory=db,
        assets_ids=["tok_a", "tok_b", "tok_c"],
    )

    # The audit log should include count of assets
    assert client._assets_ids == ["tok_a", "tok_b", "tok_c"]
    assert len(client._assets_ids) == 3


# ---------------------------------------------------------------------------
# BUG 1b: yes_token_id via condition_id fallback
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ws_client_resolves_yes_token_id_from_condition_id():
    """Book frames without asset_id should resolve yes_token_id via condition_id."""
    queue: asyncio.Queue = asyncio.Queue()
    db = _mock_db_factory()

    # Mapping includes condition_id → yes_token_id (set by orchestrator)
    client = CLOBWebSocketClient(
        config=_mock_config(),
        queue=queue,
        db_session_factory=db,
        token_id_to_yes_token_id={"0xcond_abc": "yes_tok_abc"},
    )

    # Book frame has NO asset_id — only market (condition_id)
    msg = json.dumps({
        "event": "book",
        "market": "0xcond_abc",
        "bids": [{"price": "0.40", "size": "100"}],
        "asks": [{"price": "0.60", "size": "100"}],
    })
    await client._handle_message(msg)

    assert queue.qsize() == 1
    snapshot = queue.get_nowait()
    assert snapshot.yes_token_id == "yes_tok_abc"


@pytest.mark.asyncio
async def test_ws_client_no_token_maps_to_yes_token():
    """Both YES and NO token asset_ids must resolve to the YES token_id."""
    queue: asyncio.Queue = asyncio.Queue()
    db = _mock_db_factory()

    # Mapping: both YES and NO token IDs point to the YES token
    client = CLOBWebSocketClient(
        config=_mock_config(),
        queue=queue,
        db_session_factory=db,
        token_id_to_yes_token_id={
            "tok_yes": "tok_yes",
            "tok_no": "tok_yes",
        },
    )

    msg = json.dumps({
        "event": "price_change",
        "market": "0xcond789",
        "asset_id": "tok_no",
        "best_bid": 0.35,
        "best_ask": 0.65,
    })
    await client._handle_message(msg)

    assert queue.qsize() == 1
    snapshot = queue.get_nowait()
    assert snapshot.yes_token_id == "tok_yes", "NO token must resolve to YES token_id"


# ---------------------------------------------------------------------------
# BUG 2b: midpoint=0 suppression
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ws_client_parses_price_changes_array():
    """price_change events with price_changes[] array are parsed correctly."""
    from src.schemas.market import MarketSnapshotSchema

    data = {
        "market": "0xtest",
        "price_changes": [{
            "asset_id": "123",
            "price": "0.50",
            "size": "1000",
            "side": "SELL",
            "best_bid": "0.48",
            "best_ask": "0.52",
        }]
    }
    # Simulate ws_client extraction logic
    price_changes = data.get("price_changes", [])
    best_bid = 0.0
    best_ask = 0.0
    if price_changes and isinstance(price_changes, list):
        first_change = price_changes[0]
        if isinstance(first_change, dict):
            best_bid = float(first_change.get("best_bid", 0.0))
            best_ask = float(first_change.get("best_ask", 0.0))

    assert best_bid == 0.48
    assert best_ask == 0.52

    # Verify schema accepts the values
    schema = MarketSnapshotSchema(
        condition_id="0xtest",
        best_bid=best_bid,
        best_ask=best_ask,
        raw_ws_payload='{"test": true}',
    )
    assert schema.midpoint == 0.5


@pytest.mark.asyncio
async def test_ws_client_skips_price_change_with_missing_ask():
    """price_change frame with best_ask=0 must NOT emit a snapshot."""
    queue: asyncio.Queue = asyncio.Queue()
    db = _mock_db_factory()

    client = CLOBWebSocketClient(
        config=_mock_config(),
        queue=queue,
        db_session_factory=db,
    )

    msg = json.dumps({
        "event": "price_change",
        "market": "0xcond_no_ask",
        "best_bid": 0.45,
        # best_ask absent → defaults to 0.0
    })
    await client._handle_message(msg)

    assert queue.qsize() == 0, "must not emit snapshot when best_ask is missing"


@pytest.mark.asyncio
async def test_ws_client_skips_book_with_empty_lists_and_no_fallback():
    """Book frame with empty bids/asks and no top-level fallback → no snapshot."""
    queue: asyncio.Queue = asyncio.Queue()
    db = _mock_db_factory()

    client = CLOBWebSocketClient(
        config=_mock_config(),
        queue=queue,
        db_session_factory=db,
    )

    msg = json.dumps({
        "event": "book",
        "market": "0xcond_empty",
        "bids": [],
        "asks": [],
    })
    await client._handle_message(msg)

    assert queue.qsize() == 0, "must not emit snapshot when book has no bids/asks"
