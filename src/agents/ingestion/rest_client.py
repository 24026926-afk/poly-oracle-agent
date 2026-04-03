"""
src/agents/ingestion/rest_client.py

Gamma REST API client for fetching Polymarket market metadata.

Provides ``MarketMetadata`` for token IDs, end dates, and volume —
data not available on the CLOB WebSocket stream.
"""

import time

import httpx
import structlog

from src.core.config import AppConfig
from src.core.exceptions import RESTClientError
from src.schemas.market import MarketMetadata

logger = structlog.get_logger(__name__)

_REQUEST_TIMEOUT = httpx.Timeout(10.0)
_CACHE_TTL_S = 60.0


class GammaRESTClient:
    """Fetches and caches market metadata from the Gamma API."""

    def __init__(
        self,
        config: AppConfig,
        http_session: httpx.AsyncClient,
    ) -> None:
        self._base_url = config.gamma_api_url.rstrip("/")
        self._http = http_session
        self._cache: list[MarketMetadata] = []
        self._cache_ts: float = 0.0

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    async def get_active_markets(self) -> list[MarketMetadata]:
        """Return active markets, cached for 60 seconds."""
        now = time.monotonic()
        if self._cache and (now - self._cache_ts) < _CACHE_TTL_S:
            return self._cache

        url = (
            f"{self._base_url}/markets"
            f"?active=true&closed=false"
            f"&limit=100&order=volume24hr&ascending=false"
        )

        try:
            resp = await self._http.get(url, timeout=_REQUEST_TIMEOUT)
            if resp.status_code != 200:
                logger.warning(
                    "gamma.active_markets_error",
                    status=resp.status_code,
                )
                return self._cache  # stale is better than nothing

            raw: list[dict] = resp.json()
        except Exception as exc:
            logger.warning(
                "gamma.active_markets_failed",
                error=str(exc),
            )
            return self._cache

        if raw:
            logger.debug(
                "gamma.first_item_keys",
                keys=sorted(raw[0].keys()) if isinstance(raw[0], dict) else "non-dict",
            )

        markets: list[MarketMetadata] = []
        skipped = 0
        for item in raw:
            try:
                markets.append(MarketMetadata.model_validate(item))
            except Exception as exc:
                skipped += 1
                if skipped <= 3:
                    logger.warning(
                        "gamma.market_parse_error",
                        error=str(exc),
                        condition_id=item.get("conditionId", "?") if isinstance(item, dict) else "?",
                    )
                continue

        self._cache = markets
        self._cache_ts = time.monotonic()

        logger.debug(
            "gamma.active_markets_fetched",
            count=len(markets),
            skipped=skipped,
        )
        return markets

    async def get_market_by_condition_id(
        self, condition_id: str
    ) -> MarketMetadata | None:
        """Fetch a single market by condition ID.

        Returns ``None`` on 404.  Raises ``RESTClientError`` on 5xx.
        """
        url = f"{self._base_url}/markets/{condition_id}"

        resp = await self._http.get(url, timeout=_REQUEST_TIMEOUT)

        if resp.status_code == 404:
            return None

        if resp.status_code >= 500:
            raise RESTClientError(
                f"Gamma server error: {resp.status_code}",
                status_code=resp.status_code,
            )

        if resp.status_code != 200:
            logger.warning(
                "gamma.market_lookup_error",
                condition_id=condition_id,
                status=resp.status_code,
            )
            return None

        data: dict = resp.json()

        return MarketMetadata.model_validate(data)
