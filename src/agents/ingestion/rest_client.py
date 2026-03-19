"""
src/agents/ingestion/rest_client.py

Gamma REST API client for fetching Polymarket market metadata.

Provides ``MarketMetadata`` for token IDs, end dates, and volume —
data not available on the CLOB WebSocket stream.
"""

import time

import aiohttp
import structlog

from src.core.config import AppConfig
from src.core.exceptions import RESTClientError
from src.schemas.market import MarketMetadata

logger = structlog.get_logger(__name__)

_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)
_CACHE_TTL_S = 60.0


class GammaRESTClient:
    """Fetches and caches market metadata from the Gamma API."""

    def __init__(
        self,
        config: AppConfig,
        http_session: aiohttp.ClientSession,
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

        url = f"{self._base_url}/markets?active=true&closed=false"

        try:
            async with self._http.get(
                url, timeout=_REQUEST_TIMEOUT
            ) as resp:
                if resp.status != 200:
                    logger.warning(
                        "gamma.active_markets_error",
                        status=resp.status,
                    )
                    return self._cache  # stale is better than nothing

                raw: list[dict] = await resp.json()
        except Exception as exc:
            logger.warning(
                "gamma.active_markets_failed",
                error=str(exc),
            )
            return self._cache

        markets: list[MarketMetadata] = []
        for item in raw:
            try:
                markets.append(MarketMetadata.model_validate(item))
            except Exception:
                continue  # skip unparseable entries

        self._cache = markets
        self._cache_ts = time.monotonic()

        logger.debug(
            "gamma.active_markets_fetched",
            count=len(markets),
        )
        return markets

    async def get_market_by_condition_id(
        self, condition_id: str
    ) -> MarketMetadata | None:
        """Fetch a single market by condition ID.

        Returns ``None`` on 404.  Raises ``RESTClientError`` on 5xx.
        """
        url = f"{self._base_url}/markets/{condition_id}"

        async with self._http.get(url, timeout=_REQUEST_TIMEOUT) as resp:
            if resp.status == 404:
                return None

            if resp.status >= 500:
                body = await resp.text()
                raise RESTClientError(
                    f"Gamma server error: {resp.status}",
                    status_code=resp.status,
                )

            if resp.status != 200:
                logger.warning(
                    "gamma.market_lookup_error",
                    condition_id=condition_id,
                    status=resp.status,
                )
                return None

            data: dict = await resp.json()

        return MarketMetadata.model_validate(data)
