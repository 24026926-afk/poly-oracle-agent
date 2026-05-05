"""
src/backtesting/polymarket_history_client.py

WI-43 Historical Polymarket data client.

Fetches resolved/closed market data from public Polymarket/Gamma API
for offline backtesting dataset construction.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)

_DEFAULT_TIMEOUT = httpx.Timeout(30.0)
_MAX_RETRIES = 3
_RETRY_BACKOFF_BASE = 2.0
_GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"


class PolymarketHistoryClient:
    """HTTP client for fetching resolved Polymarket market history.

    Fetches closed/resolved markets from the Gamma API with bounded
    retry and explicit timeout. Active/open markets are excluded.
    """

    def __init__(
        self,
        http_client: httpx.AsyncClient | None = None,
        *,
        timeout: httpx.Timeout = _DEFAULT_TIMEOUT,
        max_retries: int = _MAX_RETRIES,
    ) -> None:
        self._http = http_client or httpx.AsyncClient(timeout=timeout)
        self._timeout = timeout
        self._max_retries = max_retries

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    async def fetch_resolved_markets(
        self,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Fetch closed/resolved markets from the Gamma API.

        Returns raw market dicts. Active, open, or unresolved markets
        are excluded client-side.

        Args:
            start_date: ISO-format start date filter (optional).
            end_date: ISO-format end date filter (optional).
            limit: Max markets per request.
        """
        params: dict[str, Any] = {
            "closed": "true",
            "limit": limit,
            "order": "volume24hr",
            "ascending": "false",
        }
        if start_date:
            params["startDateMin"] = start_date
        if end_date:
            params["endDateMax"] = end_date

        try:
            raw_markets = await self._get_with_retry(
                _GAMMA_EVENTS_URL,
                params=params,
            )
        except Exception as exc:
            logger.error(
                "history_client.fetch_failed",
                error=str(exc),
                start_date=start_date,
                end_date=end_date,
            )
            raise

        resolved: list[dict[str, Any]] = []
        for market in raw_markets:
            if not isinstance(market, dict):
                continue
            if market.get("closed") is True and market.get("resolved") is True:
                resolved.append(market)

        logger.info(
            "history_client.resolved_fetched",
            total_raw=len(raw_markets),
            resolved=len(resolved),
        )
        return resolved

    async def fetch_market_snapshots(
        self,
        condition_id: str,
        *,
        start_ts: int | None = None,
        end_ts: int | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch timeseries snapshots for a specific market.

        Returns raw snapshot dicts for a given condition_id.
        Raises on exhausted retries so the caller can emit a typed
        skip reason and the CLI can exit non-zero.
        """
        url = f"{_GAMMA_EVENTS_URL}/{condition_id}/timeseries"
        params: dict[str, Any] = {}
        if start_ts:
            params["startTs"] = start_ts
        if end_ts:
            params["endTs"] = end_ts
        params["fidelity"] = 1440  # daily

        snapshots = await self._get_with_retry(url, params=params)
        if not isinstance(snapshots, list):
            raise ValueError(
                f"Unexpected snapshot response type for {condition_id}: "
                f"{type(snapshots).__name__}"
            )
        return snapshots

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._http.aclose()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _get_with_retry(
        self,
        url: str,
        params: dict[str, Any],
    ) -> Any:
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                resp = await self._http.get(url, params=params, timeout=self._timeout)
                if resp.status_code == 429:
                    retry_after = float(resp.headers.get("Retry-After", "5"))
                    logger.warning(
                        "history_client.rate_limited",
                        attempt=attempt,
                        retry_after=retry_after,
                    )
                    if attempt < self._max_retries:
                        await asyncio.sleep(retry_after)
                        continue
                    raise httpx.HTTPStatusError(
                        "Rate limited after max retries",
                        request=resp.request,
                        response=resp,
                    )

                if resp.status_code >= 500:
                    logger.warning(
                        "history_client.server_error",
                        status=resp.status_code,
                        attempt=attempt,
                    )
                    if attempt < self._max_retries:
                        await asyncio.sleep(
                            _RETRY_BACKOFF_BASE ** attempt
                        )
                        continue
                    resp.raise_for_status()

                resp.raise_for_status()
                return resp.json()

            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                last_exc = exc
                if attempt < self._max_retries:
                    wait = _RETRY_BACKOFF_BASE ** attempt
                    logger.warning(
                        "history_client.retry",
                        attempt=attempt,
                        wait=wait,
                        error=str(exc),
                    )
                    await asyncio.sleep(wait)
                    continue
                raise

        if last_exc:
            raise last_exc
        raise RuntimeError("Unexpected: retry loop exhausted without exception")
