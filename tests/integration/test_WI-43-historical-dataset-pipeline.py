"""
tests/integration/test_WI-43-historical-dataset-pipeline.py

Integration tests for WI-43 Historical Polymarket Dataset Pipeline.

Verifies that the builder produces BacktestDataLoader-compatible
output using mock HTTP responses.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from src.backtesting.historical_dataset import HistoricalDatasetBuilder
from src.backtesting.polymarket_history_client import PolymarketHistoryClient
from src.backtesting.schemas import HistoricalDatasetBuildResult


@pytest.mark.asyncio
async def test_builder_produces_loader_compatible_fixture_dataset(tmp_path):
    """End-to-end: builder writes snapshot files readable by BacktestDataLoader."""
    output_dir = tmp_path / "historical_fixture"
    client = PolymarketHistoryClient()

    mock_markets = [
        {
            "id": "fixture-token-yes",
            "conditionId": "cond-fixture-001",
            "closed": True,
            "resolved": True,
            "outcome": "YES",
            "outcomePrice": "1.00",
            "realizedPnl": "25.50",
            "endDate": "2025-06-15T00:00:00Z",
        },
        {
            "id": "fixture-token-no",
            "conditionId": "cond-fixture-002",
            "closed": True,
            "resolved": True,
            "outcome": "NO",
            "outcomePrice": "0.00",
            "realizedPnl": "-10.00",
            "endDate": "2025-07-01T00:00:00Z",
        },
    ]

    async def _mock_snapshots(clob_token_id: str, **kwargs):
        if clob_token_id == "cond-fixture-001":
            return [
                {
                    "timestamp_utc": "2025-03-01T12:00:00Z",
                    "best_bid": "0.60",
                    "best_ask": "0.62",
                    "midpoint": "0.61",
                    "spread": "0.02",
                    "volume_24h": "15000",
                },
                {
                    "timestamp_utc": "2025-03-02T12:00:00Z",
                    "best_bid": "0.65",
                    "best_ask": "0.67",
                    "midpoint": "0.66",
                    "spread": "0.02",
                    "volume_24h": "18000",
                },
            ]
        elif clob_token_id == "cond-fixture-002":
            return [
                {
                    "timestamp_utc": "2025-04-01T12:00:00Z",
                    "best_bid": "0.10",
                    "best_ask": "0.12",
                    "midpoint": "0.11",
                }
            ]
        return []

    with (
        patch.object(client, "fetch_resolved_markets", return_value=mock_markets),
        patch.object(client, "fetch_market_snapshots", side_effect=_mock_snapshots),
    ):
        builder = HistoricalDatasetBuilder(
            client=client,
            output_dir=output_dir,
            start_date="2025-01-01",
            end_date="2025-12-31",
        )

        result = await builder.build()

    await client.close()

    # Validate result structure
    assert isinstance(result, HistoricalDatasetBuildResult)
    assert result.manifest.market_count == 2
    assert result.manifest.snapshot_count == 3
    assert result.manifest.skipped_count == 0
    assert result.manifest.start_date == "2025-01-01"
    assert result.manifest.end_date == "2025-12-31"

    # Validate manifest file exists
    manifest_path = output_dir / "manifest.json"
    assert manifest_path.exists()
    manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest_data["market_count"] == 2

    # Validate snapshot files exist and are readable by BacktestDataLoader
    from decimal import Decimal

    # Verify each snapshot file
    snapshot_files = sorted(
        f
        for f in output_dir.glob("*.json")
        if f.name != "manifest.json" and not f.name.endswith("_outcomes.json")
    )
    assert len(snapshot_files) > 0, "Expected at least one snapshot file"

    for sf in snapshot_files:
        data = json.loads(sf.read_text(encoding="utf-8"))
        assert isinstance(data, list), f"Expected list in {sf.name}"
        for record in data:
            assert "token_id" in record
            assert "timestamp_utc" in record
            assert "best_bid" in record
            assert "best_ask" in record
            assert "midpoint" in record
            # Verify values are Decimal-safe strings
            Decimal(record["best_bid"])
            Decimal(record["best_ask"])
            Decimal(record["midpoint"])

    # Verify outcomes files exist with resolution data
    for market in mock_markets:
        outcomes_path = output_dir / f"{market['id']}_outcomes.json"
        assert outcomes_path.exists(), f"Expected outcomes file for {market['id']}"
        outcomes_data = json.loads(outcomes_path.read_text(encoding="utf-8"))
        assert outcomes_data["token_id"] == market["id"]
        assert outcomes_data["condition_id"] == market["conditionId"]
        assert "resolved_outcome" in outcomes_data


@pytest.mark.asyncio
async def test_builder_integration_with_backtest_data_loader(tmp_path):
    """Fixture dataset can be loaded by the real BacktestDataLoader."""
    from src.backtest_runner import BacktestDataLoader
    from src.schemas.execution import BacktestConfig
    from decimal import Decimal

    output_dir = tmp_path / "loader_integration"
    client = PolymarketHistoryClient()

    mock_markets = [
        {
            "id": "loader-int-token",
            "conditionId": "cond-loader-int",
            "closed": True,
            "resolved": True,
            "outcome": "YES",
            "endDate": "2025-12-31T00:00:00Z",
        },
    ]
    mock_timeseries = [
        {
            "timestamp_utc": "2025-05-01T12:00:00Z",
            "best_bid": "0.50",
            "best_ask": "0.52",
            "midpoint": "0.51",
        },
        {
            "timestamp_utc": "2025-05-02T12:00:00Z",
            "best_bid": "0.48",
            "best_ask": "0.50",
            "midpoint": "0.49",
        },
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

    # Now load via BacktestDataLoader
    config = BacktestConfig(
        data_dir=str(output_dir),
        initial_bankroll_usdc=Decimal("1000"),
        dry_run=True,
    )
    loader = BacktestDataLoader(config=config)
    snapshots = loader.load_all()

    assert len(snapshots) == 2
    assert all(s.token_id == "loader-int-token" for s in snapshots)
    assert all(isinstance(s.best_bid, Decimal) for s in snapshots)
    assert all(isinstance(s.best_ask, Decimal) for s in snapshots)
    assert all(isinstance(s.midpoint, Decimal) for s in snapshots)


@pytest.mark.asyncio
async def test_builder_handles_empty_market_list(tmp_path):
    """Builder writes valid manifest even with zero resolved markets."""
    output_dir = tmp_path / "empty_dataset"
    client = PolymarketHistoryClient()

    with patch.object(client, "fetch_resolved_markets", return_value=[]):
        builder = HistoricalDatasetBuilder(
            client=client,
            output_dir=output_dir,
            start_date="2025-01-01",
            end_date="2025-12-31",
        )
        result = await builder.build()

    await client.close()

    assert result.manifest.market_count == 0
    assert result.manifest.snapshot_count == 0

    manifest_path = output_dir / "manifest.json"
    assert manifest_path.exists()
    manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest_data["market_count"] == 0
