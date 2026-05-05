#!/usr/bin/env python3
"""
scripts/build_historical_dataset.py

WI-43 CLI entrypoint for building historical Polymarket datasets.

Usage:
    python scripts/build_historical_dataset.py \\
        --start-date 2025-01-01 \\
        --end-date 2025-12-31 \\
        --output-dir data/historical
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import structlog

from src.backtesting.historical_dataset import HistoricalDatasetBuilder
from src.backtesting.polymarket_history_client import PolymarketHistoryClient

logger = structlog.get_logger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build historical Polymarket dataset for backtesting"
    )
    parser.add_argument(
        "--start-date",
        required=True,
        help="Start date in YYYY-MM-DD format",
    )
    parser.add_argument(
        "--end-date",
        required=True,
        help="End date in YYYY-MM-DD format",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Output directory for snapshot JSON files",
    )
    return parser.parse_args()


async def main() -> int:
    args = parse_args()

    output_dir = Path(args.output_dir)
    client = PolymarketHistoryClient()

    builder = HistoricalDatasetBuilder(
        client=client,
        output_dir=output_dir,
        start_date=args.start_date,
        end_date=args.end_date,
    )

    try:
        result = await builder.build()
    except Exception as exc:
        logger.error("build.failed", error=str(exc))
        await client.close()
        return 1
    finally:
        await client.close()

    manifest_path = output_dir / "manifest.json"
    print(f"Dataset built: {result.manifest.market_count} markets, "
          f"{result.manifest.snapshot_count} snapshots, "
          f"{result.manifest.skipped_count} skipped")
    print(f"Manifest: {manifest_path}")

    if result.source_failure:
        logger.error("build.source_failure", skipped_count=result.manifest.skipped_count)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
