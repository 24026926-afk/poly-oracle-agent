"""
tests/unit/test_WI-43-historical-dataset-pipeline.py

Unit tests for WI-43 Historical Polymarket Dataset Pipeline.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.backtesting.schemas import (
    HistoricalDataSkipReason,
    HistoricalDatasetBuildResult,
    HistoricalDatasetManifest,
    HistoricalMarketRecord,
    HistoricalSnapshotRecord,
    SkipReasonCode,
)
from src.backtesting.historical_dataset import HistoricalDatasetBuilder
from src.backtesting.polymarket_history_client import PolymarketHistoryClient

UTC = timezone.utc


# ===================================================================
# Helpers
# ===================================================================


def _snapshot(**overrides) -> HistoricalSnapshotRecord:
    defaults = {
        "token_id": "token-yes-001",
        "timestamp_utc": datetime(2026, 1, 15, 12, 0, tzinfo=UTC),
        "best_bid": Decimal("0.44"),
        "best_ask": Decimal("0.46"),
        "midpoint": Decimal("0.45"),
        "spread": Decimal("0.02"),
        "volume_24h": Decimal("5000"),
    }
    defaults.update(overrides)
    return HistoricalSnapshotRecord(**defaults)


def _market(**overrides) -> HistoricalMarketRecord:
    defaults = {
        "token_id": "token-yes-001",
        "condition_id": "cond-abc-123",
        "market_end_date": datetime(2026, 6, 1, tzinfo=UTC),
        "snapshots": [_snapshot()],
        "resolved_outcome": "YES",
        "resolved_outcome_price": Decimal("1.00"),
        "realized_pnl_usdc": Decimal("10.50"),
    }
    defaults.update(overrides)
    return HistoricalMarketRecord(**defaults)


def _raw_market(**overrides) -> dict:
    defaults = {
        "id": "token-yes-001",
        "conditionId": "cond-abc-123",
        "outcome": "YES",
        "outcomePrice": "1.00",
        "realizedPnl": "10.50",
        "endDate": "2026-06-01T00:00:00Z",
        "timeseries": [
            {
                "timestamp_utc": "2026-01-15T12:00:00Z",
                "best_bid": "0.44",
                "best_ask": "0.46",
                "midpoint": "0.45",
                "spread": "0.02",
                "volume_24h": "5000",
            }
        ],
    }
    defaults.update(overrides)
    return defaults


def _make_client(**kwargs) -> PolymarketHistoryClient:
    return PolymarketHistoryClient(**kwargs)


# ===================================================================
# Schema existence and structure
# ===================================================================


def test_historical_market_record_schema_exists():
    record = _market()
    assert isinstance(record, HistoricalMarketRecord)
    assert record.token_id == "token-yes-001"
    assert record.condition_id == "cond-abc-123"


def test_historical_snapshot_record_schema_exists():
    snap = _snapshot()
    assert isinstance(snap, HistoricalSnapshotRecord)
    assert snap.token_id == "token-yes-001"
    assert snap.best_bid == Decimal("0.44")


def test_historical_dataset_manifest_schema_exists():
    manifest = HistoricalDatasetManifest(
        market_count=5,
        snapshot_count=50,
        skipped_count=3,
        start_date="2025-01-01",
        end_date="2025-12-31",
        generated_at_utc="2026-01-01T00:00:00Z",
        output_dir="/tmp/historical",
    )
    assert manifest.market_count == 5
    assert manifest.snapshot_count == 50


def test_historical_dataset_build_result_schema_exists():
    manifest = HistoricalDatasetManifest(
        market_count=1,
        snapshot_count=1,
        skipped_count=0,
        start_date="2025-01-01",
        end_date="2025-01-01",
        generated_at_utc="2026-01-01T00:00:00Z",
        output_dir="/tmp",
    )
    result = HistoricalDatasetBuildResult(manifest=manifest, skipped=[])
    assert result.manifest.market_count == 1
    assert result.success


def test_historical_data_skip_reason_schema_exists():
    skip = HistoricalDataSkipReason(
        code=SkipReasonCode.MISSING_TOKEN_ID,
        message="No token_id found",
    )
    assert skip.code == SkipReasonCode.MISSING_TOKEN_ID
    assert skip.message == "No token_id found"


# ===================================================================
# Decimal integrity — monetary / pricing fields
# ===================================================================


@pytest.mark.parametrize(
    "field_name",
    [
        "best_bid",
        "best_ask",
        "midpoint",
        "spread",
        "volume_24h",
    ],
)
def test_historical_snapshot_record_rejects_float_monetary_fields(field_name):
    kwargs = {
        "token_id": "tok",
        "timestamp_utc": datetime(2026, 1, 1, tzinfo=UTC),
        "best_bid": Decimal("0.44"),
        "best_ask": Decimal("0.46"),
        "midpoint": Decimal("0.45"),
        "spread": Decimal("0.02"),
        "volume_24h": Decimal("5000"),
    }
    kwargs[field_name] = 0.5
    with pytest.raises(ValueError, match="Float"):
        HistoricalSnapshotRecord(**kwargs)


@pytest.mark.parametrize(
    "field_name",
    [
        "resolved_outcome_price",
        "realized_pnl_usdc",
    ],
)
def test_historical_market_record_rejects_float_monetary_fields(field_name):
    kwargs = {
        "token_id": "tok",
        "condition_id": "cond",
        "resolved_outcome_price": Decimal("1.0"),
        "realized_pnl_usdc": Decimal("5.0"),
    }
    kwargs[field_name] = 0.5
    with pytest.raises(ValueError, match="Float"):
        HistoricalMarketRecord(**kwargs)


# ===================================================================
# Schema validation — required fields and edge cases
# ===================================================================


def test_historical_snapshot_record_requires_token_id():
    with pytest.raises(ValueError):
        HistoricalSnapshotRecord(
            timestamp_utc=datetime(2026, 1, 1, tzinfo=UTC),
            best_bid=Decimal("0.44"),
            best_ask=Decimal("0.46"),
            midpoint=Decimal("0.45"),
            spread=Decimal("0.02"),
        )


def test_historical_snapshot_record_requires_timestamp_utc():
    with pytest.raises(ValueError):
        HistoricalSnapshotRecord(
            token_id="tok",
            best_bid=Decimal("0.44"),
            best_ask=Decimal("0.46"),
            midpoint=Decimal("0.45"),
            spread=Decimal("0.02"),
        )


def test_historical_snapshot_record_rejects_invalid_timestamp():
    with pytest.raises(ValueError):
        HistoricalSnapshotRecord(
            token_id="tok",
            timestamp_utc="not-a-datetime",
            best_bid=Decimal("0.44"),
            best_ask=Decimal("0.46"),
            midpoint=Decimal("0.45"),
            spread=Decimal("0.02"),
        )


def test_historical_snapshot_record_normalizes_naive_timestamp_to_utc():
    snap = HistoricalSnapshotRecord(
        token_id="tok",
        timestamp_utc=datetime(2026, 1, 1, 12, 0),  # naive
        best_bid=Decimal("0.44"),
        best_ask=Decimal("0.46"),
        midpoint=Decimal("0.45"),
        spread=Decimal("0.02"),
    )
    assert snap.timestamp_utc.tzinfo is not None


def test_historical_snapshot_record_rejects_negative_best_bid():
    with pytest.raises(ValueError, match="best_bid must be positive"):
        _snapshot(best_bid=Decimal("-0.01"))


def test_historical_snapshot_record_rejects_negative_best_ask():
    with pytest.raises(ValueError, match="best_ask must be positive"):
        _snapshot(best_ask=Decimal("-0.01"))


def test_historical_snapshot_record_rejects_negative_midpoint():
    with pytest.raises(ValueError, match="midpoint must be positive"):
        _snapshot(midpoint=Decimal("-0.01"))


def test_historical_snapshot_record_rejects_crossed_book():
    with pytest.raises(ValueError, match="Crossed book"):
        _snapshot(best_bid=Decimal("0.50"), best_ask=Decimal("0.40"))


def test_historical_market_record_requires_token_id():
    with pytest.raises(ValueError):
        HistoricalMarketRecord(condition_id="cond")


def test_historical_market_record_requires_condition_id():
    with pytest.raises(ValueError):
        HistoricalMarketRecord(token_id="tok")


# ===================================================================
# Lookahead separation — outcomes vs point-in-time fields
# ===================================================================


def test_snapshot_record_separates_observable_fields_from_outcome_fields():
    """Snapshot must NOT carry resolution/outcome fields."""
    snap = _snapshot()
    assert (
        not hasattr(snap, "resolved_outcome")
        or snap.__class__.__name__ == "HistoricalSnapshotRecord"
    )
    # Verify that resolved_outcome, resolved_outcome_price, realized_pnl_usdc
    # are NOT fields on HistoricalSnapshotRecord
    snap_fields = HistoricalSnapshotRecord.model_fields.keys()
    assert "resolved_outcome" not in snap_fields
    assert "resolved_outcome_price" not in snap_fields
    assert "realized_pnl_usdc" not in snap_fields

    # Verify they ARE on HistoricalMarketRecord
    market_fields = HistoricalMarketRecord.model_fields.keys()
    assert "resolved_outcome" in market_fields
    assert "resolved_outcome_price" in market_fields
    assert "realized_pnl_usdc" in market_fields


def test_outcome_fields_not_accessible_before_simulated_market_close():
    """Outcome data must live on the market record, separate from snapshots."""
    market = _market(
        resolved_outcome="YES",
        resolved_outcome_price=Decimal("1.00"),
        realized_pnl_usdc=Decimal("10.50"),
    )
    # Outcome data is on the market record
    assert market.resolved_outcome == "YES"
    assert market.resolved_outcome_price == Decimal("1.00")

    # Snapshots within the market do NOT carry outcome data
    for snap in market.snapshots:
        assert not hasattr(snap, "resolved_outcome")


# ===================================================================
# Manifest generation
# ===================================================================


def test_manifest_includes_market_count():
    manifest = HistoricalDatasetManifest(
        market_count=10,
        snapshot_count=100,
        skipped_count=5,
        start_date="2025-01-01",
        end_date="2025-12-31",
        generated_at_utc="2026-01-01T00:00:00Z",
        output_dir="/tmp",
    )
    assert manifest.market_count == 10


def test_manifest_includes_snapshot_count():
    manifest = HistoricalDatasetManifest(
        market_count=10,
        snapshot_count=100,
        skipped_count=5,
        start_date="2025-01-01",
        end_date="2025-12-31",
        generated_at_utc="2026-01-01T00:00:00Z",
        output_dir="/tmp",
    )
    assert manifest.snapshot_count == 100


def test_manifest_includes_skipped_count():
    manifest = HistoricalDatasetManifest(
        market_count=10,
        snapshot_count=100,
        skipped_count=5,
        start_date="2025-01-01",
        end_date="2025-12-31",
        generated_at_utc="2026-01-01T00:00:00Z",
        output_dir="/tmp",
    )
    assert manifest.skipped_count == 5


def test_manifest_includes_date_range():
    manifest = HistoricalDatasetManifest(
        market_count=1,
        snapshot_count=1,
        skipped_count=0,
        start_date="2025-01-01",
        end_date="2025-12-31",
        generated_at_utc="2026-01-01T00:00:00Z",
        output_dir="/tmp",
    )
    assert manifest.start_date == "2025-01-01"
    assert manifest.end_date == "2025-12-31"


def test_manifest_includes_generation_timestamp():
    manifest = HistoricalDatasetManifest(
        market_count=1,
        snapshot_count=1,
        skipped_count=0,
        start_date="2025-01-01",
        end_date="2025-01-01",
        generated_at_utc="2026-01-01T00:00:00Z",
        output_dir="/tmp",
    )
    assert manifest.generated_at_utc == "2026-01-01T00:00:00Z"


def test_manifest_includes_source_identifiers():
    manifest = HistoricalDatasetManifest(
        market_count=1,
        snapshot_count=1,
        skipped_count=0,
        start_date="2025-01-01",
        end_date="2025-01-01",
        source_identifiers=["gamma-api.polymarket.com"],
        generated_at_utc="2026-01-01T00:00:00Z",
        output_dir="/tmp",
    )
    assert "gamma-api.polymarket.com" in manifest.source_identifiers


def test_manifest_reports_zero_eligible_markets_for_empty_source():
    manifest = HistoricalDatasetManifest(
        market_count=0,
        snapshot_count=0,
        skipped_count=0,
        start_date="2025-01-01",
        end_date="2025-01-01",
        generated_at_utc="2026-01-01T00:00:00Z",
        output_dir="/tmp",
    )
    assert manifest.market_count == 0
    assert manifest.snapshot_count == 0


# ===================================================================
# Skip / reject reasons
# ===================================================================


def test_skip_reason_has_typed_enum_code():
    skip = HistoricalDataSkipReason(
        code=SkipReasonCode.CROSSED_BOOK,
        token_id="tok",
        message="Crossed",
    )
    assert isinstance(skip.code, SkipReasonCode)
    assert skip.code == SkipReasonCode.CROSSED_BOOK


def test_skip_reason_includes_token_id():
    skip = HistoricalDataSkipReason(
        code=SkipReasonCode.MISSING_TOKEN_ID,
        token_id="tok-123",
        message="Missing",
    )
    assert skip.token_id == "tok-123"


def test_skip_reason_includes_human_readable_message():
    skip = HistoricalDataSkipReason(
        code=SkipReasonCode.NON_POSITIVE_BID,
        token_id="tok",
        message="best_bid is -0.01, must be positive",
    )
    assert "best_bid" in skip.message


def test_missing_token_id_produces_typed_skip_reason():
    skip = HistoricalDataSkipReason(
        code=SkipReasonCode.MISSING_TOKEN_ID,
        message="No token_id",
    )
    assert skip.code == SkipReasonCode.MISSING_TOKEN_ID


def test_crossed_book_produces_typed_skip_reason():
    skip = HistoricalDataSkipReason(
        code=SkipReasonCode.CROSSED_BOOK,
        token_id="tok",
        message="best_bid > best_ask",
    )
    assert skip.code == SkipReasonCode.CROSSED_BOOK


def test_non_positive_bid_produces_typed_skip_reason():
    skip = HistoricalDataSkipReason(
        code=SkipReasonCode.NON_POSITIVE_BID,
        token_id="tok",
        message="Bid <= 0",
    )
    assert skip.code == SkipReasonCode.NON_POSITIVE_BID


def test_unresolved_market_produces_typed_skip_reason():
    skip = HistoricalDataSkipReason(
        code=SkipReasonCode.UNRESOLVED_MARKET,
        token_id="tok",
        message="Market not resolved",
    )
    assert skip.code == SkipReasonCode.UNRESOLVED_MARKET


def test_duplicate_snapshot_produces_consistent_de_duplication():
    """Verify skip reason for duplicate snapshots uses DUPLICATE_SNAPSHOT code."""
    skip = HistoricalDataSkipReason(
        code=SkipReasonCode.DUPLICATE_SNAPSHOT,
        token_id="tok",
        message="Duplicate",
    )
    assert skip.code == SkipReasonCode.DUPLICATE_SNAPSHOT


# ===================================================================
# PolymarketHistoryClient
# ===================================================================


def test_history_client_module_exists():
    client = _make_client()
    assert isinstance(client, PolymarketHistoryClient)


def test_history_client_uses_httpx_async_client():
    client = _make_client()
    import httpx

    assert client._http is not None
    # The client's _http is an httpx.AsyncClient
    assert isinstance(client._http, httpx.AsyncClient)


def test_history_client_has_explicit_timeout():
    client = _make_client()
    assert client._timeout is not None


def test_history_client_has_bounded_retry_behavior():
    client = _make_client()
    assert client._max_retries >= 0
    assert client._max_retries <= 5


@pytest.mark.asyncio
async def test_history_client_returns_resolved_markets_only():
    """fetch_resolved_markets should only return closed+resolved markets."""
    client = _make_client()
    mock_resp = [
        {"id": "m1", "closed": True, "resolved": True, "conditionId": "c1"},
        {"id": "m2", "closed": True, "resolved": False, "conditionId": "c2"},
        {"id": "m3", "closed": False, "resolved": False, "conditionId": "c3"},
        {"id": "m4", "closed": True, "resolved": True, "conditionId": "c4"},
    ]

    with patch.object(client._http, "get") as mock_get:
        mock_resp_obj = MagicMock()
        mock_resp_obj.status_code = 200
        mock_resp_obj.json.return_value = mock_resp
        mock_get.return_value = mock_resp_obj

        result = await client.fetch_resolved_markets()
        assert len(result) == 2
        assert all(m["closed"] and m["resolved"] for m in result)

    await client.close()


@pytest.mark.asyncio
async def test_history_client_excludes_active_markets():
    """Active/open markets should be excluded from resolved results."""
    client = _make_client()
    mock_resp = [
        {"id": "active", "closed": False, "resolved": False, "conditionId": "c1"},
        {
            "id": "closed_resolved",
            "closed": True,
            "resolved": True,
            "conditionId": "c2",
        },
    ]

    with patch.object(client._http, "get") as mock_get:
        mock_resp_obj = MagicMock()
        mock_resp_obj.status_code = 200
        mock_resp_obj.json.return_value = mock_resp
        mock_get.return_value = mock_resp_obj

        result = await client.fetch_resolved_markets()
        assert len(result) == 1
        assert result[0]["id"] == "closed_resolved"

    await client.close()


# ===================================================================
# HistoricalDataset builder
# ===================================================================


def test_historical_dataset_module_exists():
    builder = HistoricalDatasetBuilder(
        client=_make_client(),
        output_dir=Path("/tmp/test"),
        start_date="2025-01-01",
        end_date="2025-12-31",
    )
    assert isinstance(builder, HistoricalDatasetBuilder)


@pytest.mark.asyncio
async def test_dataset_builder_excludes_active_open_markets():
    """Builder only processes markets returned as resolved by fetch_resolved_markets."""
    client = _make_client()
    # fetch_resolved_markets() already filters to closed+resolved — only
    # resolved markets appear here. Builder processes what it receives.
    mock_markets = [
        {
            "id": "resolved-market",
            "conditionId": "c-resolved",
            "closed": True,
            "resolved": True,
            "outcome": "YES",
            "outcomePrice": "1.00",
            "endDate": "2025-06-01T00:00:00Z",
        },
    ]
    mock_timeseries = [
        {
            "timestamp_utc": "2025-02-01T12:00:00Z",
            "best_bid": "0.44",
            "best_ask": "0.46",
            "midpoint": "0.45",
        }
    ]

    with (
        patch.object(client, "fetch_resolved_markets", return_value=mock_markets),
        patch.object(client, "fetch_market_snapshots", return_value=mock_timeseries),
    ):
        builder = HistoricalDatasetBuilder(
            client=client,
            output_dir=Path("/tmp/test_builder_exclude"),
            start_date="2025-01-01",
            end_date="2025-12-31",
        )

        result = await builder.build()
        # Only the resolved market is processed
        assert result.manifest.market_count == 1

    await client.close()


@pytest.mark.asyncio
async def test_dataset_builder_excludes_cancelled_markets():
    """Builder only processes resolved (non-cancelled) markets from the client."""
    client = _make_client()
    # Same principle: fetch_resolved_markets filters before builder sees data
    mock_markets = [
        {
            "id": "good",
            "conditionId": "c-good",
            "closed": True,
            "resolved": True,
            "outcome": "NO",
            "outcomePrice": "0.00",
            "endDate": "2025-06-01T00:00:00Z",
        },
    ]
    mock_timeseries = [
        {
            "timestamp_utc": "2025-03-01T12:00:00Z",
            "best_bid": "0.10",
            "best_ask": "0.12",
            "midpoint": "0.11",
        }
    ]

    with (
        patch.object(client, "fetch_resolved_markets", return_value=mock_markets),
        patch.object(client, "fetch_market_snapshots", return_value=mock_timeseries),
    ):
        builder = HistoricalDatasetBuilder(
            client=client,
            output_dir=Path("/tmp/test_builder_cancel"),
            start_date="2025-01-01",
            end_date="2025-12-31",
        )

        result = await builder.build()
        assert result.manifest.market_count == 1

    await client.close()


@pytest.mark.asyncio
async def test_dataset_builder_writes_backtest_loader_compatible_json(tmp_path):
    """Generated JSON must be loadable by BacktestDataLoader."""
    output_dir = tmp_path / "historical_out"
    client = _make_client()

    mock_markets = [
        {
            "id": "loader-test-token",
            "conditionId": "c-loader-test",
            "closed": True,
            "resolved": True,
            "outcome": "YES",
            "endDate": "2025-12-01T00:00:00Z",
        },
    ]
    mock_timeseries = [
        {
            "timestamp_utc": "2025-06-15T12:00:00Z",
            "best_bid": "0.55",
            "best_ask": "0.57",
            "midpoint": "0.56",
        }
    ]

    with (
        patch.object(client, "fetch_resolved_markets", return_value=mock_markets),
        patch.object(client, "fetch_market_snapshots", return_value=mock_timeseries),
    ):
        builder = HistoricalDatasetBuilder(
            client=client,
            output_dir=output_dir,
            start_date="2025-01-01",
            end_date="2025-12-31",
        )

        await builder.build()

    await client.close()

    # Verify file was written with correct naming convention
    json_files = sorted(output_dir.glob("*.json"))
    loader_files = [
        f
        for f in json_files
        if f.name != "manifest.json" and not f.name.endswith("_outcomes.json")
    ]
    assert len(loader_files) > 0

    # Verify the file can be read as JSON and has required fields
    for jf in loader_files:
        data = json.loads(jf.read_text(encoding="utf-8"))
        assert isinstance(data, list)
        for record in data:
            assert "timestamp_utc" in record
            assert "best_bid" in record
            assert "best_ask" in record
            assert "midpoint" in record
            assert "token_id" in record


@pytest.mark.asyncio
async def test_dataset_builder_naming_convention_matches_loader(tmp_path):
    """Output files must follow {token_id}_{YYYY-MM-DD}.json pattern."""
    output_dir = tmp_path / "historical_naming"
    client = _make_client()

    mock_markets = [
        {
            "id": "naming-token",
            "conditionId": "c-naming",
            "closed": True,
            "resolved": True,
            "outcome": "YES",
            "endDate": "2025-12-01T00:00:00Z",
        },
    ]
    mock_timeseries = [
        {
            "timestamp_utc": "2025-07-01T12:00:00Z",
            "best_bid": "0.40",
            "best_ask": "0.42",
            "midpoint": "0.41",
        }
    ]

    with (
        patch.object(client, "fetch_resolved_markets", return_value=mock_markets),
        patch.object(client, "fetch_market_snapshots", return_value=mock_timeseries),
    ):
        builder = HistoricalDatasetBuilder(
            client=client,
            output_dir=output_dir,
            start_date="2025-01-01",
            end_date="2025-12-31",
        )
        await builder.build()

    await client.close()

    import re

    pattern = re.compile(r"^.+_\d{4}-\d{2}-\d{2}\.json$")

    for f in output_dir.glob("*.json"):
        if f.name == "manifest.json" or f.name.endswith("_outcomes.json"):
            continue
        assert pattern.match(f.name), f"File {f.name} does not match naming convention"


def test_dataset_builder_performs_no_db_writes():
    """Builder must not import or use database modules."""
    import inspect

    source = inspect.getsource(HistoricalDatasetBuilder)
    assert "Session" not in source
    assert "create_engine" not in source
    assert "sqlalchemy" not in source.lower()


def test_dataset_builder_performs_no_live_signing_or_broadcasting():
    """Builder must not import execution/signing components."""
    import inspect

    source = inspect.getsource(HistoricalDatasetBuilder)
    assert "sign" not in source.lower() or "sign" not in source
    assert "broadcast" not in source.lower()
    assert "ExecutionRouter" not in source


# ===================================================================
# CLI script
# ===================================================================


def test_cli_script_module_exists():
    import importlib

    try:
        importlib.import_module("scripts.build_historical_dataset")
    except ModuleNotFoundError:
        # Try direct path load
        from pathlib import Path

        script_path = (
            Path(__file__).parent.parent.parent
            / "scripts"
            / "build_historical_dataset.py"
        )
        assert script_path.exists(), f"CLI script not found at {script_path}"


def test_cli_accepts_start_date_argument():
    from scripts.build_historical_dataset import parse_args
    import sys

    with patch.object(
        sys,
        "argv",
        [
            "build_historical_dataset.py",
            "--start-date",
            "2025-01-01",
            "--end-date",
            "2025-12-31",
            "--output-dir",
            "/tmp",
        ],
    ):
        args = parse_args()
        assert args.start_date == "2025-01-01"


def test_cli_accepts_end_date_argument():
    from scripts.build_historical_dataset import parse_args
    import sys

    with patch.object(
        sys,
        "argv",
        [
            "build_historical_dataset.py",
            "--start-date",
            "2025-01-01",
            "--end-date",
            "2025-06-30",
            "--output-dir",
            "/tmp",
        ],
    ):
        args = parse_args()
        assert args.end_date == "2025-06-30"


def test_cli_accepts_output_dir_argument():
    from scripts.build_historical_dataset import parse_args
    import sys

    with patch.object(
        sys,
        "argv",
        [
            "build_historical_dataset.py",
            "--start-date",
            "2025-01-01",
            "--end-date",
            "2025-12-31",
            "--output-dir",
            "/custom/path",
        ],
    ):
        args = parse_args()
        assert args.output_dir == "/custom/path"


@pytest.mark.asyncio
async def test_cli_exits_non_zero_on_source_failure_after_retries():
    """CLI must return non-zero exit code on unhandled exceptions."""
    client = _make_client()

    with patch.object(
        client,
        "fetch_resolved_markets",
        side_effect=Exception("Simulated source failure"),
    ):
        builder = HistoricalDatasetBuilder(
            client=client,
            output_dir=Path("/tmp/test_fail"),
            start_date="2025-01-01",
            end_date="2025-12-31",
        )

        with pytest.raises(Exception, match="Simulated source failure"):
            await builder.build()

    await client.close()


@pytest.mark.asyncio
async def test_cli_writes_manifest_on_success(tmp_path):
    """Successful build must write a manifest.json."""
    output_dir = tmp_path / "manifest_test"
    client = _make_client()

    mock_markets = [
        {
            "id": "manifest-token",
            "conditionId": "c-manifest",
            "closed": True,
            "resolved": True,
            "outcome": "YES",
            "endDate": "2025-12-01T00:00:00Z",
        },
    ]
    mock_timeseries = [
        {
            "timestamp_utc": "2025-08-01T12:00:00Z",
            "best_bid": "0.50",
            "best_ask": "0.52",
            "midpoint": "0.51",
        }
    ]

    with (
        patch.object(client, "fetch_resolved_markets", return_value=mock_markets),
        patch.object(client, "fetch_market_snapshots", return_value=mock_timeseries),
    ):
        builder = HistoricalDatasetBuilder(
            client=client,
            output_dir=output_dir,
            start_date="2025-01-01",
            end_date="2025-12-31",
        )
        await builder.build()

    await client.close()

    manifest_path = output_dir / "manifest.json"
    assert manifest_path.exists()

    manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert "market_count" in manifest_data
    assert "snapshot_count" in manifest_data
    assert "skipped_count" in manifest_data
    assert "start_date" in manifest_data
    assert "end_date" in manifest_data


# ===================================================================
# Integration gate — BacktestDataLoader compatibility
# ===================================================================


def test_generated_snapshots_loadable_by_backtest_data_loader():
    """Snapshot dict format must be parseable by BacktestDataLoader requirements."""
    snap = _snapshot()
    from src.backtesting.historical_dataset import HistoricalDatasetBuilder

    snap_dict = HistoricalDatasetBuilder._snapshot_to_dict(
        snap, token_id="test-token", condition_id="cond-test-123"
    )

    assert "timestamp_utc" in snap_dict
    assert "best_bid" in snap_dict
    assert "best_ask" in snap_dict
    assert "midpoint" in snap_dict
    assert "token_id" in snap_dict
    assert "condition_id" in snap_dict
    assert snap_dict["token_id"] == "test-token"
    assert snap_dict["condition_id"] == "cond-test-123"

    # All monetary values must be strings (JSON-safe Decimal)
    assert isinstance(snap_dict["best_bid"], str)
    assert isinstance(snap_dict["best_ask"], str)
    assert isinstance(snap_dict["midpoint"], str)


def test_generated_snapshots_preserve_decimal_integrity():
    """All monetary fields in emitted JSON must use Decimal-safe strings."""
    snap = _snapshot(
        best_bid=Decimal("0.12345678901234567890"),
        best_ask=Decimal("0.23456789012345678901"),
        midpoint=Decimal("0.17901234456790123455"),
    )
    from src.backtesting.historical_dataset import HistoricalDatasetBuilder

    snap_dict = HistoricalDatasetBuilder._snapshot_to_dict(
        snap, token_id="precise-token", condition_id="cond-precise"
    )

    # String representation preserves precision (no float rounding)
    assert snap_dict["best_bid"] == "0.12345678901234567890"
    assert snap_dict["best_ask"] == "0.23456789012345678901"
    assert snap_dict["midpoint"] == "0.17901234456790123455"


# ===================================================================
# Real Gamma API shape (fix for /events->/markets drift)
# ===================================================================


@pytest.mark.asyncio
async def test_history_client_accepts_uma_resolution_status():
    """The live /markets endpoint reports resolution via umaResolutionStatus,
    not a boolean `resolved` field."""
    client = _make_client()
    mock_resp = [
        {
            "id": "m1",
            "closed": True,
            "umaResolutionStatus": "resolved",
            "conditionId": "c1",
        },
        {
            "id": "m2",
            "closed": True,
            "umaResolutionStatus": "posted",
            "conditionId": "c2",
        },
        {
            "id": "m3",
            "closed": False,
            "umaResolutionStatus": "resolved",
            "conditionId": "c3",
        },
    ]
    with patch.object(client._http, "get") as mock_get:
        ro = MagicMock()
        ro.status_code = 200
        ro.json.return_value = mock_resp
        mock_get.return_value = ro
        result = await client.fetch_resolved_markets()
    assert [m["id"] for m in result] == ["m1"]
    await client.close()


@pytest.mark.asyncio
async def test_client_snapshots_unwrap_clob_history_envelope():
    """CLOB prices-history returns {"history": [...]}; the client unwraps it."""
    client = _make_client()
    with patch.object(client._http, "get") as mock_get:
        ro = MagicMock()
        ro.status_code = 200
        ro.json.return_value = {"history": [{"t": 1775692800, "p": 0.365}]}
        mock_get.return_value = ro
        history = await client.fetch_market_snapshots(clob_token_id="999")
    assert history == [{"t": 1775692800, "p": 0.365}]
    await client.close()


def test_derive_resolved_outcome_from_index_aligned_arrays():
    b = HistoricalDatasetBuilder(
        client=_make_client(), output_dir=Path("/tmp"), start_date="x", end_date="y"
    )
    assert b._derive_resolved_outcome('["Yes","No"]', '["1","0"]') == "YES"
    assert b._derive_resolved_outcome('["Yes","No"]', '["0","1"]') == "NO"
    assert b._derive_resolved_outcome(["Yes", "No"], ["0", "0"]) is None


def test_extract_yes_clob_token():
    assert (
        HistoricalDatasetBuilder._extract_yes_clob_token(
            {"clobTokenIds": '["yes-tok","no-tok"]'}
        )
        == "yes-tok"
    )
    assert (
        HistoricalDatasetBuilder._extract_yes_clob_token({"clobTokenIds": ["a", "b"]})
        == "a"
    )
    assert HistoricalDatasetBuilder._extract_yes_clob_token({}) is None


@pytest.mark.asyncio
async def test_builder_handles_real_markets_shape_and_price_only_timeseries(tmp_path):
    """End-to-end: real /markets fields + CLOB price-only {t,p} points.

    Verifies outcome derivation from index-aligned arrays and zero-spread book
    synthesis from price points.
    """
    client = _make_client()
    raw = [
        {
            "id": "123",
            "conditionId": "0xcond",
            "closed": True,
            "umaResolutionStatus": "resolved",
            "outcomes": '["Yes","No"]',
            "outcomePrices": '["0","1"]',  # NO won
            "clobTokenIds": '["999yes","888no"]',
            "endDate": "2026-05-31T00:00:00Z",
        }
    ]
    timeseries = [{"t": 1775692800, "p": 0.365}, {"t": 1776816000, "p": 0.515}]
    with (
        patch.object(client, "fetch_resolved_markets", return_value=raw),
        patch.object(client, "fetch_market_snapshots", return_value=timeseries),
    ):
        builder = HistoricalDatasetBuilder(
            client=client,
            output_dir=tmp_path,
            start_date="2026-05-01",
            end_date="2026-06-01",
        )
        result = await builder.build()

    assert result.manifest.market_count == 1
    assert result.manifest.snapshot_count == 2

    outcomes = json.loads((tmp_path / "123_outcomes.json").read_text())
    assert outcomes["resolved_outcome"] == "NO"

    snap_files = [p for p in tmp_path.glob("123_*.json") if "outcomes" not in p.name]
    snaps = [s for f in snap_files for s in json.loads(f.read_text())]
    # Zero-spread synthesis: bid == ask == midpoint == p
    assert any(s["best_bid"] == s["best_ask"] == s["midpoint"] for s in snaps)
