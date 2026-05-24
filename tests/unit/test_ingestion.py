"""
tests/unit/test_ingestion.py

Async unit tests for Module 1 — Market Ingestion Engine.
"""

import asyncio
import json
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.agents.ingestion.rest_client import GammaRESTClient
from src.agents.ingestion.ws_client import CLOBWebSocketClient
from src.schemas.market import MarketMetadata, MarketSnapshotSchema


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _book_frame(**overrides: object) -> str:
    """Return a valid CLOB book frame as raw JSON."""
    base = {
        "event": "book",
        "market": "0xcondition123",
        "best_bid": 0.45,
        "best_ask": 0.55,
        "last_trade_price": 0.50,
        "outcome_token": "YES",
        "question": "Will it rain?",
    }
    base.update(overrides)
    return json.dumps(base)


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


def _mock_config(
    *,
    snapshot_persist_min_bps: int = 25,
    snapshot_persist_max_interval_sec: Decimal = Decimal("2.0"),
) -> MagicMock:
    cfg = MagicMock()
    cfg.clob_ws_url = "wss://fake.ws/market"
    cfg.gamma_api_url = "https://gamma-api.fake.com"
    cfg.snapshot_persist_min_bps = snapshot_persist_min_bps
    cfg.snapshot_persist_max_interval_sec = snapshot_persist_max_interval_sec
    return cfg


class _FakeResponse:
    """Minimal httpx response mock for REST client tests."""

    def __init__(self, status_code: int, body: object) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> object:
        return self._body

    @property
    def text(self) -> str:
        return json.dumps(self._body)


# ---------------------------------------------------------------------------
# WebSocket Client Tests
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ws_message_valid_book_frame_enqueues_snapshot():
    queue: asyncio.Queue = asyncio.Queue()
    db = _mock_db_factory()
    client = CLOBWebSocketClient(_mock_config(), queue, db)

    await client._handle_message(_book_frame())

    assert queue.qsize() == 1
    row = queue.get_nowait()
    assert row.condition_id == "0xcondition123"
    assert row.midpoint == Decimal("0.5")


@pytest.mark.asyncio
async def test_ws_pong_response_is_handled():
    """Server PONG responses should be handled silently without enqueueing."""
    queue: asyncio.Queue = asyncio.Queue()
    db = _mock_db_factory()
    client = CLOBWebSocketClient(_mock_config(), queue, db)

    # Should not raise or log errors
    await client._handle_message("PONG")
    await client._handle_message(" PONG ")  # whitespace-tolerant

    assert queue.qsize() == 0


@pytest.mark.asyncio
async def test_ws_unknown_message_type_is_ignored():
    queue: asyncio.Queue = asyncio.Queue()
    db = _mock_db_factory()
    client = CLOBWebSocketClient(_mock_config(), queue, db)

    await client._handle_message(json.dumps({"event": "heartbeat_ack"}))

    assert queue.qsize() == 0


@pytest.mark.asyncio
async def test_ws_invalid_json_does_not_crash():
    queue: asyncio.Queue = asyncio.Queue()
    db = _mock_db_factory()
    client = CLOBWebSocketClient(_mock_config(), queue, db)

    # Should not raise
    await client._handle_message("this is not json {{{")

    assert queue.qsize() == 0


@pytest.mark.asyncio
async def test_ws_validation_error_skips_frame():
    queue: asyncio.Queue = asyncio.Queue()
    db = _mock_db_factory()
    client = CLOBWebSocketClient(_mock_config(), queue, db)

    # best_bid = 5.0 exceeds le=1.0 constraint → ValidationError
    bad_frame = _book_frame(best_bid=5.0)
    await client._handle_message(bad_frame)

    assert queue.qsize() == 0


@pytest.mark.asyncio
async def test_ws_midpoint_computed_not_trusted():
    """Midpoint must be (bid + ask) / 2, never the externally-provided value."""
    schema = MarketSnapshotSchema(
        condition_id="abc",
        best_bid=0.40,
        best_ask=0.60,
        midpoint=0.99,  # garbage — must be overwritten
        raw_ws_payload="{}",
    )
    assert schema.midpoint == Decimal("0.5")


# ---------------------------------------------------------------------------
# Gamma REST Client Tests
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_gamma_get_active_markets_returns_list():
    body = [
        {
            "conditionId": "cond1",
            "question": "Q1",
            "clobTokenIds": ["t1", "t2"],
            "active": True,
            "closed": False,
        }
    ]
    http = MagicMock()
    http.get = AsyncMock(return_value=_FakeResponse(200, body))

    client = GammaRESTClient(_mock_config(), http)
    result = await client.get_active_markets()

    assert len(result) == 1
    assert result[0].condition_id == "cond1"


@pytest.mark.asyncio
async def test_gamma_cache_returns_stale_within_60s():
    body = [
        {
            "conditionId": "cond1",
            "question": "Q1",
            "clobTokenIds": [],
            "active": True,
            "closed": False,
        }
    ]
    http = MagicMock()
    http.get = AsyncMock(return_value=_FakeResponse(200, body))

    client = GammaRESTClient(_mock_config(), http)

    first = await client.get_active_markets()
    # Second call should use cache — http.get should be called only once
    second = await client.get_active_markets()

    assert first == second
    # httpx coroutine: if cache works, .get is awaited only once.
    assert http.get.call_count == 1


@pytest.mark.asyncio
async def test_gamma_404_returns_none():
    http = MagicMock()
    http.get = AsyncMock(return_value=_FakeResponse(404, {}))

    client = GammaRESTClient(_mock_config(), http)
    result = await client.get_market_by_condition_id("nonexistent")

    assert result is None


def test_market_metadata_normalizes_gamma_category_and_tags():
    market = MarketMetadata.model_validate(
        {
            "conditionId": "cond-category",
            "question": "Will BTC exceed $100k?",
            "category": "Crypto",
            "tags": [{"label": "btc"}, {"slug": "crypto"}],
            "clobTokenIds": ["yes-token", "no-token"],
            "active": True,
            "closed": False,
        }
    )

    assert market.category == "CRYPTO"
    assert market.tags == ["btc", "crypto"]


def test_market_metadata_allows_missing_category():
    market = MarketMetadata.model_validate(
        {
            "conditionId": "cond-missing-category",
            "question": "Will it rain?",
            "clobTokenIds": ["yes-token", "no-token"],
            "active": True,
            "closed": False,
        }
    )

    assert market.category is None
    assert market.tags == []


# ---------------------------------------------------------------------------
# Gamma query parameter validation — ensures robust market discovery
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_gamma_query_includes_limit_and_volume_sort():
    """The active-markets URL must request a capped, volume-sorted page
    so the Gamma API returns the most liquid markets (not an empty page)."""
    body = [
        {
            "conditionId": "cond1",
            "question": "Q1",
            "clobTokenIds": ["t1"],
            "active": True,
            "closed": False,
            "volume24hr": 50000.0,
        }
    ]
    http = MagicMock()
    http.get = AsyncMock(return_value=_FakeResponse(200, body))

    client = GammaRESTClient(_mock_config(), http)
    await client.get_active_markets()

    called_url: str = http.get.call_args[0][0]

    # Must include pagination limit
    assert "limit=" in called_url, "query must include a limit parameter"
    # Must sort by 24h volume descending for liquidity
    assert "order=volume24hr" in called_url, "query must sort by volume24hr"
    assert "ascending=false" in called_url, "query must use descending order"
    # Must still filter for active, non-closed
    assert "active=true" in called_url
    assert "closed=false" in called_url


@pytest.mark.asyncio
async def test_gamma_query_has_no_restrictive_tag_or_category_filters():
    """The active-markets URL must NOT contain tag= or category= params
    that could exclude valid high-volume markets."""
    body = []
    http = MagicMock()
    http.get = AsyncMock(return_value=_FakeResponse(200, body))

    client = GammaRESTClient(_mock_config(), http)
    await client.get_active_markets()

    called_url: str = http.get.call_args[0][0]

    assert "tag=" not in called_url, "must not filter by tag"
    assert "category=" not in called_url, "must not filter by category"


# ---------------------------------------------------------------------------
# WebSocket subscription format
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ws_subscription_uses_assets_ids_and_logs_message():
    """The subscription message must use 'assets_ids' (token IDs),
    not 'market_ids', and the ws_client must accept token IDs at init."""
    queue: asyncio.Queue = asyncio.Queue()
    db = _mock_db_factory()
    token_ids = ["tok_yes_123", "tok_no_456"]
    client = CLOBWebSocketClient(_mock_config(), queue, db, assets_ids=token_ids)

    # Build the subscription message the client would send
    sub_msg = client._build_subscription_message()
    parsed = json.loads(sub_msg)

    assert parsed["type"] == "market"
    assert parsed["custom_feature_enabled"] is True
    assert "assets_ids" in parsed, "must use 'assets_ids', not 'market_ids'"
    assert parsed["assets_ids"] == token_ids
    assert "market_ids" not in parsed, "must NOT contain 'market_ids'"


@pytest.mark.asyncio
async def test_ws_handles_list_response_without_crashing():
    """The CLOB WS may send list-wrapped messages like [{...}] or batches.
    _handle_message must not crash with 'list has no attribute get'."""
    queue: asyncio.Queue = asyncio.Queue()
    db = _mock_db_factory()
    client = CLOBWebSocketClient(_mock_config(), queue, db)

    # Single-item list wrapping a valid book event
    list_msg = json.dumps(
        [
            {
                "event": "book",
                "market": "0xcondition_list",
                "best_bid": 0.40,
                "best_ask": 0.60,
                "last_trade_price": 0.50,
                "outcome_token": "YES",
                "question": "List test?",
            }
        ]
    )
    await client._handle_message(list_msg)

    assert queue.qsize() == 1
    row = queue.get_nowait()
    assert row.condition_id == "0xcondition_list"


@pytest.mark.asyncio
async def test_ws_handles_empty_list_response_gracefully():
    """An empty list [] from the WS must not crash."""
    queue: asyncio.Queue = asyncio.Queue()
    db = _mock_db_factory()
    client = CLOBWebSocketClient(_mock_config(), queue, db)

    await client._handle_message("[]")

    assert queue.qsize() == 0


@pytest.mark.asyncio
async def test_ws_handles_multi_item_list_processes_all():
    """A batch of events in a list should each be processed."""
    queue: asyncio.Queue = asyncio.Queue()
    db = _mock_db_factory()
    client = CLOBWebSocketClient(_mock_config(), queue, db)

    batch = json.dumps(
        [
            {
                "event": "book",
                "market": "0xmarket_a",
                "best_bid": 0.30,
                "best_ask": 0.70,
                "last_trade_price": 0.50,
                "outcome_token": "YES",
                "question": "Q1?",
            },
            {
                "event": "book",
                "market": "0xmarket_b",
                "best_bid": 0.45,
                "best_ask": 0.55,
                "last_trade_price": 0.50,
                "outcome_token": "NO",
                "question": "Q2?",
            },
        ]
    )
    await client._handle_message(batch)

    assert queue.qsize() == 2


@pytest.mark.asyncio
async def test_ws_handles_non_json_server_error_gracefully():
    """Non-JSON server responses like 'INVALID OPERATION' must be logged
    as server errors, not generic invalid_json."""
    queue: asyncio.Queue = asyncio.Queue()
    db = _mock_db_factory()
    client = CLOBWebSocketClient(_mock_config(), queue, db)

    with patch("src.agents.ingestion.ws_client.logger") as mock_logger:
        await client._handle_message("INVALID OPERATION")

        # Should log as a server error, not just invalid_json
        mock_logger.warning.assert_called_once()
        call_args = mock_logger.warning.call_args
        assert call_args[0][0] == "ws_client.server_error"


# ---------------------------------------------------------------------------
# Gamma API real response shape — clobTokenIds is a JSON-encoded string
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_gamma_parses_stringified_clob_token_ids():
    """The real Gamma API returns clobTokenIds as a JSON-encoded STRING,
    e.g. '["tok1", "tok2"]', NOT a native list. Markets must still parse."""
    body = [
        {
            "conditionId": "0xabc123",
            "question": "Will X happen?",
            "clobTokenIds": '["tok_yes", "tok_no"]',  # STRING, not list
            "endDateIso": "2026-06-01",
            "active": True,
            "closed": False,
            "volume24hr": 1000000.0,
        }
    ]
    http = MagicMock()
    http.get = AsyncMock(return_value=_FakeResponse(200, body))

    client = GammaRESTClient(_mock_config(), http)
    result = await client.get_active_markets()

    assert len(result) == 1, "stringified clobTokenIds must not silently drop markets"
    assert result[0].condition_id == "0xabc123"
    assert result[0].token_ids == ["tok_yes", "tok_no"]


@pytest.mark.asyncio
async def test_gamma_parses_native_list_clob_token_ids():
    """Backwards compat: if clobTokenIds is already a native list, still works."""
    body = [
        {
            "conditionId": "0xdef456",
            "question": "Will Y happen?",
            "clobTokenIds": ["tok_a", "tok_b"],  # native list
            "endDateIso": "2026-06-01",
            "active": True,
            "closed": False,
            "volume24hr": 500000.0,
        }
    ]
    http = MagicMock()
    http.get = AsyncMock(return_value=_FakeResponse(200, body))

    client = GammaRESTClient(_mock_config(), http)
    result = await client.get_active_markets()

    assert len(result) == 1
    assert result[0].token_ids == ["tok_a", "tok_b"]


# ---------------------------------------------------------------------------
# F4 (2026-05-23): WS snapshot persistence throttle
# ---------------------------------------------------------------------------
def _make_ws_client_for_throttle(
    *,
    min_bps: int = 25,
    max_interval_sec: Decimal = Decimal("2.0"),
) -> CLOBWebSocketClient:
    queue: asyncio.Queue = asyncio.Queue()
    db = _mock_db_factory()
    cfg = _mock_config(
        snapshot_persist_min_bps=min_bps,
        snapshot_persist_max_interval_sec=max_interval_sec,
    )
    return CLOBWebSocketClient(cfg, queue, db)


def test_should_persist_first_snapshot_always_returns_true():
    client = _make_ws_client_for_throttle()
    assert (
        client._should_persist_snapshot("0xcond_a", Decimal("0.5"), Decimal("100.0"))
        is True
    )


def test_should_persist_same_midpoint_within_window_returns_false():
    client = _make_ws_client_for_throttle()
    # First persist seeds state
    client._last_persist["0xcond_a"] = (Decimal("100.0"), Decimal("0.5"))
    # 0.5s later, same midpoint, window=2.0s, min_bps=25
    assert (
        client._should_persist_snapshot("0xcond_a", Decimal("0.5"), Decimal("100.5"))
        is False
    )


def test_should_persist_midpoint_change_above_min_bps_returns_true():
    client = _make_ws_client_for_throttle(min_bps=25)
    client._last_persist["0xcond_a"] = (Decimal("100.0"), Decimal("0.50"))
    # Δmidpoint = 0.005, prev=0.50 → 100 bps > 25 bps
    assert (
        client._should_persist_snapshot("0xcond_a", Decimal("0.505"), Decimal("100.5"))
        is True
    )


def test_should_persist_time_window_elapsed_forces_persist_even_if_midpoint_unchanged():
    client = _make_ws_client_for_throttle(max_interval_sec=Decimal("2.0"))
    client._last_persist["0xcond_a"] = (Decimal("100.0"), Decimal("0.5"))
    # 2.5s later, same midpoint → time window forces persist
    assert (
        client._should_persist_snapshot("0xcond_a", Decimal("0.5"), Decimal("102.5"))
        is True
    )


def test_should_persist_zero_prev_midpoint_defaults_to_true_without_division_error():
    client = _make_ws_client_for_throttle()
    # Degenerate previous midpoint (0) — must not divide by zero, must persist.
    client._last_persist["0xcond_a"] = (Decimal("100.5"), Decimal("0"))
    assert (
        client._should_persist_snapshot("0xcond_a", Decimal("0.5"), Decimal("100.7"))
        is True
    )


@pytest.mark.asyncio
async def test_ws_persistence_throttle_skips_repeated_same_midpoint_but_still_enqueues():
    """Eval queue is fed every frame; DB insert is throttled within the window."""
    queue: asyncio.Queue = asyncio.Queue()
    db = _mock_db_factory()
    cfg = _mock_config(
        snapshot_persist_min_bps=25,
        snapshot_persist_max_interval_sec=Decimal("60.0"),  # long window
    )
    client = CLOBWebSocketClient(cfg, queue, db)

    # Same identical frame twice in quick succession
    await client._handle_message(_book_frame())
    await client._handle_message(_book_frame())

    # Both frames feed the in-memory queue (eval cadence unchanged).
    assert queue.qsize() == 2

    # But only the first one persists; the session is built once.
    # (_mock_db_factory returns the same session every call; commit is called
    # exactly once across both frames because the second was throttled.)
    session = db._last_session
    assert session.commit.await_count == 1
    assert session.add.call_count == 1


# ---------------------------------------------------------------------------
# F5/F6 (2026-05-23): per-condition burst markers
# ---------------------------------------------------------------------------
def _price_change_no_token_frame(
    *,
    market: str = "0xcond_degen",
    no_token_id: str = "no-token-x",
    yes_token_id: str = "yes-token-x",
    bid: float = 0.999,
    ask: float = 1.0,
) -> str:
    """Frame that triggers skip_no_token_non_positive_yes_quote when NO-side
    is registered: YES-normalized bid/ask collapse to <=0."""
    return json.dumps({
        "event": "price_change",
        "market": market,
        "asset_id": no_token_id,
        "price_changes": [{"best_bid": bid, "best_ask": ask}],
    })


def _last_trade_frame(market: str = "0xcond_warmup", price: float = 0.5) -> str:
    return json.dumps({
        "event": "last_trade_price",
        "market": market,
        "price": price,
    })


def _book_frame_for(market: str, best_bid: float = 0.45, best_ask: float = 0.55) -> str:
    return json.dumps({
        "event": "book",
        "market": market,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "outcome_token": "YES",
    })


@pytest.mark.asyncio
async def test_ws_degenerate_quote_first_detected_emits_once_per_condition():
    """F5: first degenerate NO-quote per condition emits INFO; subsequent stay DEBUG."""
    queue: asyncio.Queue = asyncio.Queue()
    db = _mock_db_factory()
    client = CLOBWebSocketClient(_mock_config(), queue, db)
    # Register the NO-token mapping so the NO-side branch fires.
    client.set_token_id_mapping({"no-token-x": "yes-token-x"})
    client.set_market_token_pairs({"0xcond_degen": ("yes-token-x", "no-token-x")})

    with patch("src.agents.ingestion.ws_client.logger") as mock_logger:
        # 3 identical degenerate frames
        for _ in range(3):
            await client._handle_message(_price_change_no_token_frame())
        info_events = [
            c.args[0] if c.args else c.kwargs.get("event", "")
            for c in mock_logger.info.call_args_list
        ]
        debug_events = [
            c.args[0] if c.args else c.kwargs.get("event", "")
            for c in mock_logger.debug.call_args_list
        ]

    assert info_events.count("ws_client.degenerate_quote_first_detected") == 1
    assert debug_events.count("ws_client.skip_no_token_non_positive_yes_quote") == 3
    assert client._degenerate_quote_conditions == {"0xcond_degen"}


@pytest.mark.asyncio
async def test_ws_degenerate_quote_distinct_conditions_each_emit_info():
    """F5: two distinct degenerate conditions each get one INFO marker."""
    queue: asyncio.Queue = asyncio.Queue()
    db = _mock_db_factory()
    client = CLOBWebSocketClient(_mock_config(), queue, db)
    client.set_token_id_mapping(
        {"no-token-a": "yes-token-a", "no-token-b": "yes-token-b"}
    )
    client.set_market_token_pairs({
        "0xcond_a": ("yes-token-a", "no-token-a"),
        "0xcond_b": ("yes-token-b", "no-token-b"),
    })

    with patch("src.agents.ingestion.ws_client.logger") as mock_logger:
        await client._handle_message(
            _price_change_no_token_frame(market="0xcond_a", no_token_id="no-token-a", yes_token_id="yes-token-a")
        )
        await client._handle_message(
            _price_change_no_token_frame(market="0xcond_b", no_token_id="no-token-b", yes_token_id="yes-token-b")
        )
        info_events = [
            c.args[0] if c.args else c.kwargs.get("event", "")
            for c in mock_logger.info.call_args_list
        ]

    assert info_events.count("ws_client.degenerate_quote_first_detected") == 2


@pytest.mark.asyncio
async def test_ws_book_warmup_complete_emits_once_after_pre_book_trades():
    """F6: pre-book trades, then first valid book, emits one INFO with suppression count."""
    queue: asyncio.Queue = asyncio.Queue()
    db = _mock_db_factory()
    client = CLOBWebSocketClient(_mock_config(), queue, db)

    with patch("src.agents.ingestion.ws_client.logger") as mock_logger:
        # 5 pre-book last_trade_price frames — all skipped, counted.
        for _ in range(5):
            await client._handle_message(_last_trade_frame(market="0xcond_warmup"))
        # Then the first valid book — should trigger book_warmup_complete.
        await client._handle_message(_book_frame_for("0xcond_warmup"))
        # Another valid book — should NOT re-trigger book_warmup_complete.
        await client._handle_message(_book_frame_for("0xcond_warmup"))

        info_events = [
            c.args[0] if c.args else c.kwargs.get("event", "")
            for c in mock_logger.info.call_args_list
        ]
        debug_events = [
            c.args[0] if c.args else c.kwargs.get("event", "")
            for c in mock_logger.debug.call_args_list
        ]

    assert info_events.count("ws_client.book_warmup_complete") == 1
    assert debug_events.count("ws_client.skip_last_trade_no_book") == 5
    assert client._pre_book_trades_by_condition["0xcond_warmup"] == 5
    assert "0xcond_warmup" in client._book_warmup_complete


@pytest.mark.asyncio
async def test_ws_book_warmup_complete_not_emitted_without_pre_book_trades():
    """F6: condition that never had pre-book trades does not emit book_warmup_complete."""
    queue: asyncio.Queue = asyncio.Queue()
    db = _mock_db_factory()
    client = CLOBWebSocketClient(_mock_config(), queue, db)

    with patch("src.agents.ingestion.ws_client.logger") as mock_logger:
        # First message is a valid book — no preceding last_trade_price frames.
        await client._handle_message(_book_frame_for("0xcond_clean"))
        info_events = [
            c.args[0] if c.args else c.kwargs.get("event", "")
            for c in mock_logger.info.call_args_list
        ]

    assert "ws_client.book_warmup_complete" not in info_events
    assert "0xcond_clean" not in client._book_warmup_complete
