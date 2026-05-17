"""
src/backtesting/historical_dataset.py

WI-43 Historical dataset builder.

Transforms raw Polymarket history API responses into validated
BacktestDataLoader-compatible JSON snapshot files with lookahead-safe
separation of observable fields from resolution/outcome data.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import structlog

from src.backtesting.polymarket_history_client import PolymarketHistoryClient
from src.backtesting.schemas import (
    HistoricalDataSkipReason,
    HistoricalDatasetBuildResult,
    HistoricalDatasetManifest,
    HistoricalMarketRecord,
    HistoricalSnapshotRecord,
    SkipReasonCode,
)

logger = structlog.get_logger(__name__)


class HistoricalDatasetBuilder:
    """Builds BacktestDataLoader-compatible historical datasets.

    Discovers resolved markets via fetch_resolved_markets(), then
    fetches per-market timeseries via fetch_market_snapshots() for
    each condition_id. Validates every record through Pydantic schemas
    and writes:

      - ``{token_id}_{YYYY-MM-DD}.json`` — BacktestDataLoader-compatible
        point-in-time snapshots (observable fields only).
      - ``{token_id}_outcomes.json`` — Market-level resolution/outcome
        data kept separate to prevent lookahead leakage.

    Resolution/outcome fields are stored on HistoricalMarketRecord
    separately from point-in-time HistoricalSnapshotRecords.
    """

    def __init__(
        self,
        client: PolymarketHistoryClient,
        *,
        output_dir: Path,
        start_date: str,
        end_date: str,
    ) -> None:
        self._client = client
        self._output_dir = output_dir
        self._start_date = start_date
        self._end_date = end_date

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    async def build(self) -> HistoricalDatasetBuildResult:
        """Execute the full dataset build pipeline.

        1. Discover resolved markets via fetch_resolved_markets().
        2. For each market, fetch timeseries via fetch_market_snapshots().
        3. Parse, validate, de-duplicate, and write output files.
        4. Write manifest and return typed result.
        """
        self._output_dir.mkdir(parents=True, exist_ok=True)

        raw_markets = await self._client.fetch_resolved_markets(
            start_date=self._start_date,
            end_date=self._end_date,
        )

        skipped: list[HistoricalDataSkipReason] = []
        markets: list[HistoricalMarketRecord] = []
        total_snapshots = 0
        source_failure = False

        for idx, raw in enumerate(raw_markets):
            (
                record,
                record_skips,
                record_source_failure,
            ) = await self._build_market_record(raw, index=idx)
            skipped.extend(record_skips)
            if record_source_failure:
                source_failure = True
            if record is not None:
                # Skip markets that produced zero valid snapshots
                if len(record.snapshots) == 0:
                    skip = HistoricalDataSkipReason(
                        code=SkipReasonCode.MALFORMED_RECORD,
                        token_id=record.token_id,
                        condition_id=record.condition_id,
                        message="Market has zero valid snapshots after parsing",
                        record_index=idx,
                    )
                    skipped.append(skip)
                    logger.warning(
                        "builder.market_skipped",
                        reason=skip.code.value,
                        token_id=record.token_id,
                        condition_id=record.condition_id,
                        message=skip.message,
                    )
                    continue
                markets.append(record)
                snapshot_count = self._write_snapshot_files(record)
                total_snapshots += snapshot_count
                self._write_outcomes_file(record)

        manifest = HistoricalDatasetManifest(
            market_count=len(markets),
            snapshot_count=total_snapshots,
            skipped_count=len(skipped),
            start_date=self._start_date,
            end_date=self._end_date,
            source_identifiers=["gamma-api.polymarket.com"],
            generated_at_utc=datetime.now(timezone.utc).isoformat(),
            output_dir=str(self._output_dir),
        )

        self._write_manifest(manifest)

        logger.info(
            "dataset_builder.complete",
            market_count=len(markets),
            snapshot_count=total_snapshots,
            skipped_count=len(skipped),
        )

        return HistoricalDatasetBuildResult(
            manifest=manifest,
            skipped=skipped,
            source_failure=source_failure,
        )

    # ------------------------------------------------------------------
    # Market-level build (discovery + timeseries fetch)
    # ------------------------------------------------------------------

    async def _build_market_record(
        self,
        raw: dict[str, Any],
        *,
        index: int,
    ) -> tuple[
        HistoricalMarketRecord | None,
        list[HistoricalDataSkipReason],
        bool,  # source_failure
    ]:
        """Fetch timeseries for a resolved market and parse into typed record.

        Returns (record, skips, source_failure).  source_failure is True
        when the snapshot fetch itself failed (exhausted retries), which
        should cause the CLI to exit non-zero.
        """
        skips: list[HistoricalDataSkipReason] = []

        token_id = raw.get("id") or raw.get("tokenId") or ""
        condition_id = raw.get("conditionId") or raw.get("condition_id") or ""

        if not token_id:
            skip = HistoricalDataSkipReason(
                code=SkipReasonCode.MISSING_TOKEN_ID,
                message="Market record missing token_id/id field",
                record_index=index,
            )
            skips.append(skip)
            logger.warning(
                "builder.market_skipped",
                reason=skip.code.value,
                record_index=index,
                message=skip.message,
            )
            return None, skips, False

        if not condition_id:
            skip = HistoricalDataSkipReason(
                code=SkipReasonCode.MISSING_CONDITION_ID,
                token_id=token_id,
                message="Market record missing condition_id",
                record_index=index,
            )
            skips.append(skip)
            logger.warning(
                "builder.market_skipped",
                reason=skip.code.value,
                token_id=token_id,
                message=skip.message,
            )
            return None, skips, False

        # Parse outcome fields from the resolved market record
        resolved_outcome = raw.get("outcome") or raw.get("resolvedOutcome")
        resolved_outcome_price = self._parse_optional_decimal(
            raw.get("outcomePrice") or raw.get("resolvedOutcomePrice"),
            field_name="resolved_outcome_price",
            token_id=token_id,
            skips=skips,
        )
        realized_pnl_usdc = self._parse_optional_decimal(
            raw.get("realizedPnl") or raw.get("realizedPnlUsdc"),
            field_name="realized_pnl_usdc",
            token_id=token_id,
            skips=skips,
        )

        # Reject markets with missing or ambiguous resolved outcomes
        if not resolved_outcome or resolved_outcome not in ("YES", "NO"):
            skip = HistoricalDataSkipReason(
                code=SkipReasonCode.AMBIGUOUS_OUTCOME,
                token_id=token_id,
                condition_id=condition_id,
                message=(
                    f"Market has missing or ambiguous resolved outcome: "
                    f"{resolved_outcome!r}"
                ),
                record_index=index,
            )
            skips.append(skip)
            logger.warning(
                "builder.market_skipped",
                reason=skip.code.value,
                token_id=token_id,
                condition_id=condition_id,
                message=skip.message,
            )
            return None, skips, False

        # Parse market end date
        market_end_date = self._parse_optional_datetime(
            raw.get("endDate") or raw.get("marketEndDate") or raw.get("closeTime"),
            token_id=token_id,
            skips=skips,
        )

        # Fetch per-market timeseries via the dedicated client method
        try:
            raw_snapshots = await self._client.fetch_market_snapshots(
                condition_id=condition_id,
            )
        except Exception as exc:
            logger.warning(
                "builder.snapshot_fetch_failed",
                condition_id=condition_id,
                error=str(exc),
            )
            skips.append(
                HistoricalDataSkipReason(
                    code=SkipReasonCode.SOURCE_ERROR,
                    token_id=token_id,
                    condition_id=condition_id,
                    message=f"Failed to fetch timeseries for {condition_id}: {exc}",
                    record_index=index,
                )
            )
            return None, skips, True

        if not isinstance(raw_snapshots, list):
            skip = HistoricalDataSkipReason(
                code=SkipReasonCode.MALFORMED_RECORD,
                token_id=token_id,
                condition_id=condition_id,
                message=(
                    f"Snapshot payload is not a list: {type(raw_snapshots).__name__}"
                ),
                record_index=index,
            )
            skips.append(skip)
            logger.warning(
                "builder.snapshot_payload_malformed",
                token_id=token_id,
                condition_id=condition_id,
                payload_type=type(raw_snapshots).__name__,
            )
            raw_snapshots = []

        # Parse and validate each snapshot
        snapshots: list[HistoricalSnapshotRecord] = []
        seen_keys: set[tuple[str, datetime]] = set()

        for snap_idx, snap_raw in enumerate(raw_snapshots):
            if not isinstance(snap_raw, dict):
                skip = HistoricalDataSkipReason(
                    code=SkipReasonCode.MALFORMED_RECORD,
                    token_id=token_id,
                    condition_id=condition_id,
                    message=f"Snapshot {snap_idx} is not a dict",
                    record_index=snap_idx,
                )
                skips.append(skip)
                logger.warning(
                    "builder.snapshot_skipped",
                    reason=skip.code.value,
                    token_id=token_id,
                    condition_id=condition_id,
                    snap_idx=snap_idx,
                    message=skip.message,
                )
                continue

            snapshot, snap_skip = self._parse_snapshot(
                snap_raw,
                token_id=token_id,
                condition_id=condition_id,
                index=snap_idx,
                skips=skips,
            )
            if snapshot is None:
                if snap_skip:
                    skips.append(snap_skip)
                    logger.warning(
                        "builder.snapshot_skipped",
                        reason=snap_skip.code.value,
                        token_id=token_id,
                        condition_id=condition_id,
                        snap_idx=snap_idx,
                        message=snap_skip.message,
                    )
                continue

            # De-duplicate
            dedup_key = (snapshot.token_id, snapshot.timestamp_utc)
            if dedup_key in seen_keys:
                skip = HistoricalDataSkipReason(
                    code=SkipReasonCode.DUPLICATE_SNAPSHOT,
                    token_id=token_id,
                    condition_id=condition_id,
                    message=(
                        f"Duplicate snapshot at {snapshot.timestamp_utc.isoformat()}"
                    ),
                    record_index=snap_idx,
                )
                skips.append(skip)
                logger.warning(
                    "builder.snapshot_skipped",
                    reason=skip.code.value,
                    token_id=token_id,
                    condition_id=condition_id,
                    snap_idx=snap_idx,
                    message=skip.message,
                )
                continue
            seen_keys.add(dedup_key)
            snapshots.append(snapshot)

        try:
            record = HistoricalMarketRecord(
                token_id=token_id,
                condition_id=condition_id,
                market_end_date=market_end_date,
                snapshots=snapshots,
                resolved_outcome=resolved_outcome,
                resolved_outcome_price=resolved_outcome_price,
                realized_pnl_usdc=realized_pnl_usdc,
            )
        except Exception as exc:
            skip = HistoricalDataSkipReason(
                code=SkipReasonCode.MALFORMED_RECORD,
                token_id=token_id,
                condition_id=condition_id,
                message=f"Failed to construct HistoricalMarketRecord: {exc}",
                record_index=index,
            )
            skips.append(skip)
            logger.warning(
                "builder.market_skipped",
                reason=skip.code.value,
                token_id=token_id,
                condition_id=condition_id,
                error=str(exc),
                message=skip.message,
            )
            return None, skips, False

        return record, skips, False

    def _parse_snapshot(
        self,
        raw: dict[str, Any],
        *,
        token_id: str,
        condition_id: str,
        index: int,
        skips: list[HistoricalDataSkipReason] | None = None,
    ) -> tuple[HistoricalSnapshotRecord | None, HistoricalDataSkipReason | None]:
        """Parse a raw timeseries snapshot dict into a validated record."""
        timestamp = self._parse_snapshot_timestamp(raw, token_id, index)
        if timestamp is None:
            return None, HistoricalDataSkipReason(
                code=SkipReasonCode.INVALID_TIMESTAMP,
                token_id=token_id,
                condition_id=condition_id,
                message=f"Invalid or missing timestamp in snapshot {index}",
                record_index=index,
            )

        try:
            best_bid = self._require_decimal(raw, "best_bid", "bestBid", "bid")
            best_ask = self._require_decimal(raw, "best_ask", "bestAsk", "ask")
            midpoint = self._require_decimal(
                raw, "midpoint", "midpoint_price", "midPrice", "price"
            )
        except ValueError as exc:
            return None, HistoricalDataSkipReason(
                code=SkipReasonCode.MALFORMED_RECORD,
                token_id=token_id,
                condition_id=condition_id,
                message=f"Missing required price field in snapshot {index}: {exc}",
                record_index=index,
            )

        spread = self._optional_decimal(
            raw,
            token_id=token_id,
            condition_id=condition_id,
            field_names=("spread",),
            default=best_ask - best_bid,
            skips=skips,
        )
        volume_24h = self._optional_decimal(
            raw,
            token_id=token_id,
            condition_id=condition_id,
            field_names=("volume_24h", "volume24hr", "volume"),
            default=None,
            skips=skips,
        )

        try:
            return HistoricalSnapshotRecord(
                token_id=token_id,
                timestamp_utc=timestamp,
                best_bid=best_bid,
                best_ask=best_ask,
                midpoint=midpoint,
                spread=spread if spread is not None else (best_ask - best_bid),
                volume_24h=volume_24h,
            ), None
        except ValueError as exc:
            reason_code = SkipReasonCode.MALFORMED_RECORD
            msg = str(exc)
            if "Crossed book" in msg:
                reason_code = SkipReasonCode.CROSSED_BOOK
            elif "must be positive" in msg:
                if "best_bid" in msg:
                    reason_code = SkipReasonCode.NON_POSITIVE_BID
                elif "best_ask" in msg:
                    reason_code = SkipReasonCode.NON_POSITIVE_ASK
                elif "midpoint" in msg:
                    reason_code = SkipReasonCode.NON_POSITIVE_MIDPOINT

            return None, HistoricalDataSkipReason(
                code=reason_code,
                token_id=token_id,
                condition_id=condition_id,
                message=f"Snapshot {index} validation failed: {exc}",
                record_index=index,
            )

    # ------------------------------------------------------------------
    # File output
    # ------------------------------------------------------------------

    def _write_snapshot_files(self, record: HistoricalMarketRecord) -> int:
        """Write BacktestDataLoader-compatible JSON files, grouped by date.

        Each record includes token_id, condition_id, timestamp_utc, best_bid,
        best_ask, midpoint, spread, volume_24h, and market_end_date — all
        point-in-time observable fields. Resolution/outcome data is NOT
        included here.

        Returns the number of snapshot records written.
        """
        grouped: dict[str, list[dict[str, Any]]] = {}
        for snapshot in record.snapshots:
            date_key = snapshot.timestamp_utc.strftime("%Y-%m-%d")
            grouped.setdefault(date_key, []).append(
                self._snapshot_to_dict(
                    snapshot,
                    token_id=record.token_id,
                    condition_id=record.condition_id,
                    market_end_date=record.market_end_date,
                )
            )

        count = 0
        for date_key, snapshots in grouped.items():
            filename = f"{record.token_id}_{date_key}.json"
            file_path = self._output_dir / filename

            existing: list[dict[str, Any]] = []
            if file_path.exists():
                try:
                    existing = json.loads(file_path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    existing = []

            merged = existing + snapshots
            file_path.write_text(
                json.dumps(merged, indent=2, default=str),
                encoding="utf-8",
            )
            count += len(snapshots)

        return count

    @staticmethod
    def _snapshot_to_dict(
        snapshot: HistoricalSnapshotRecord,
        *,
        token_id: str,
        condition_id: str,
        market_end_date: datetime | None = None,
    ) -> dict[str, Any]:
        """Convert a snapshot record to a BacktestDataLoader-compatible dict.

        Includes token_id, condition_id, and all point-in-time observable
        fields required by WI-43/WI-44. Outcome fields are excluded — they
        live in the separate outcomes file.
        """
        result: dict[str, Any] = {
            "token_id": token_id,
            "condition_id": condition_id,
            "timestamp_utc": snapshot.timestamp_utc.isoformat(),
            "best_bid": str(snapshot.best_bid),
            "best_ask": str(snapshot.best_ask),
            "midpoint": str(snapshot.midpoint),
            "spread": str(snapshot.spread),
        }
        if market_end_date is not None:
            result["market_end_date"] = market_end_date.isoformat()
        if snapshot.volume_24h is not None:
            result["volume_24h"] = str(snapshot.volume_24h)
        return result

    def _write_outcomes_file(self, record: HistoricalMarketRecord) -> None:
        """Write per-market resolution/outcome data to a separate file.

        Kept apart from point-in-time snapshots to prevent lookahead leakage
        in pre-resolution prompt context.
        """
        outcome_data: dict[str, Any] = {
            "token_id": record.token_id,
            "condition_id": record.condition_id,
        }
        if record.market_end_date is not None:
            outcome_data["market_end_date"] = record.market_end_date.isoformat()
        if record.resolved_outcome is not None:
            outcome_data["resolved_outcome"] = record.resolved_outcome
        if record.resolved_outcome_price is not None:
            outcome_data["resolved_outcome_price"] = str(record.resolved_outcome_price)
        if record.realized_pnl_usdc is not None:
            outcome_data["realized_pnl_usdc"] = str(record.realized_pnl_usdc)

        outcome_path = self._output_dir / f"{record.token_id}_outcomes.json"
        outcome_path.write_text(
            json.dumps(outcome_data, indent=2, default=str),
            encoding="utf-8",
        )
        logger.debug(
            "builder.outcomes_written",
            token_id=record.token_id,
            path=str(outcome_path),
        )

    def _write_manifest(self, manifest: HistoricalDatasetManifest) -> None:
        """Write the dataset manifest to the output directory."""
        manifest_path = self._output_dir / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest.model_dump(), indent=2, default=str),
            encoding="utf-8",
        )
        logger.info("dataset_builder.manifest_written", path=str(manifest_path))

    # ------------------------------------------------------------------
    # Field helpers
    # ------------------------------------------------------------------

    def _require_decimal(self, raw: dict[str, Any], *field_names: str) -> Decimal:
        """Extract first available named field and parse as Decimal."""
        for name in field_names:
            value = raw.get(name)
            if value is not None:
                if isinstance(value, float):
                    raise ValueError(
                        f"Float value forbidden for field '{name}': {value}"
                    )
                if isinstance(value, Decimal):
                    return value
                try:
                    return Decimal(str(value))
                except Exception:
                    pass
        raise ValueError(f"None of the fields {field_names} found in snapshot")

    def _optional_decimal(
        self,
        raw: dict[str, Any],
        *,
        token_id: str,
        condition_id: str,
        field_names: tuple[str, ...],
        default: Decimal | None = None,
        skips: list[HistoricalDataSkipReason] | None = None,
    ) -> Decimal | None:
        """Extract optional Decimal field from first available named field.

        Floats are rejected with a ``FLOAT_REJECTED`` skip reason when
        ``skips`` is provided, ensuring full Decimal auditability.
        """
        for name in field_names:
            value = raw.get(name)
            if value is not None:
                if isinstance(value, float):
                    if skips is not None:
                        skips.append(
                            HistoricalDataSkipReason(
                                code=SkipReasonCode.FLOAT_REJECTED,
                                token_id=token_id,
                                condition_id=condition_id,
                                message=(
                                    f"Float rejected for optional field "
                                    f"'{name}': {value}"
                                ),
                            )
                        )
                    return default
                if isinstance(value, Decimal):
                    return value
                try:
                    return Decimal(str(value))
                except Exception:
                    continue
        return default

    def _parse_optional_decimal(
        self,
        value: Any,
        *,
        field_name: str,
        token_id: str,
        skips: list[HistoricalDataSkipReason],
    ) -> Decimal | None:
        """Parse optional outcome Decimal field with skip tracking.

        Floats are rejected with a typed ``FLOAT_REJECTED`` skip reason.
        """
        if value is None:
            return None
        if isinstance(value, float):
            skips.append(
                HistoricalDataSkipReason(
                    code=SkipReasonCode.FLOAT_REJECTED,
                    token_id=token_id,
                    message=f"Float rejected for {field_name}: {value}",
                )
            )
            return None
        if isinstance(value, Decimal):
            return value
        try:
            return Decimal(str(value))
        except Exception:
            return None

    def _parse_optional_datetime(
        self,
        value: Any,
        *,
        token_id: str,
        skips: list[HistoricalDataSkipReason],
    ) -> datetime | None:
        """Parse optional datetime field."""
        if value is None:
            return None
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    return parsed.replace(tzinfo=timezone.utc)
                return parsed
            except ValueError:
                return None
        return None

    def _parse_snapshot_timestamp(
        self,
        raw: dict[str, Any],
        token_id: str,
        index: int,
    ) -> datetime | None:
        """Extract timestamp from snapshot raw dict."""
        ts_raw = (
            raw.get("timestamp_utc")
            or raw.get("timestamp")
            or raw.get("t")
            or raw.get("ts")
            or raw.get("time")
        )
        if ts_raw is None:
            return None
        if isinstance(ts_raw, datetime):
            if ts_raw.tzinfo is None:
                return ts_raw.replace(tzinfo=timezone.utc)
            return ts_raw
        if isinstance(ts_raw, (int, float)):
            try:
                return datetime.fromtimestamp(float(ts_raw), tz=timezone.utc)
            except (ValueError, OSError):
                return None
        if isinstance(ts_raw, str):
            try:
                parsed = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    return parsed.replace(tzinfo=timezone.utc)
                return parsed
            except ValueError:
                return None
        return None
