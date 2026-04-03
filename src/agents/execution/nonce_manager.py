"""
src/agents/execution/nonce_manager.py

Async-safe nonce manager for Polymarket CLOB order sequencing.

This is the ONLY component allowed to track and dispense on-chain order
nonces.  Its ``asyncio.Lock`` guarantee prevents duplicate / colliding
orders under concurrent execution.
"""

import asyncio

import structlog
from web3 import AsyncWeb3

from src.core.exceptions import NonceManagerError

logger = structlog.get_logger(__name__)


class NonceManager:
    """
    Dispenses monotonically increasing nonces under an asyncio lock.

    Lifecycle:
        1. Construct with an ``AsyncWeb3`` instance and wallet address.
        2. Call ``await initialize()`` once at startup.
        3. Call ``await get_next_nonce()`` before each order signing.
        4. Call ``await sync()`` after a tx revert or RPC error.
    """

    def __init__(
        self, w3: AsyncWeb3, address: str, dry_run: bool = False,
    ) -> None:
        self._w3 = w3
        self._address: str = AsyncWeb3.to_checksum_address(address)
        self._nonce: int = -1  # sentinel — not yet fetched
        self._lock: asyncio.Lock = asyncio.Lock()
        self._dry_run: bool = dry_run

    # -- public properties --------------------------------------------------

    @property
    def current_nonce(self) -> int:
        """Read-only diagnostic. Do NOT use for signing — use get_next_nonce."""
        return self._nonce

    # -- lifecycle ----------------------------------------------------------

    async def initialize(self) -> None:
        """Fetch the current tx count from Polygon RPC (pending pool).

        Must be called exactly once before any ``get_next_nonce()`` call.
        In dry-run mode, skips the RPC call and sets nonce to 0.
        """
        if self._dry_run:
            self._nonce = 0
            logger.info("nonce_manager.initialized_dry_run", nonce=0)
            return

        try:
            self._nonce = await self._w3.eth.get_transaction_count(
                self._address, "pending"
            )
        except Exception as exc:
            raise NonceManagerError(
                f"Failed to fetch initial nonce for {self._address}",
                cause=exc,
            ) from exc

        logger.info(
            "nonce_manager.initialized",
            address=self._address,
            nonce=self._nonce,
        )

    async def get_next_nonce(self) -> int:
        """Return the current nonce and post-increment under lock.

        Raises:
            RuntimeError: If ``initialize()`` has not been called yet.
        """
        if self._dry_run:
            logger.info("nonce_manager.dry_run_skip", dry_run=True)
            return -1

        async with self._lock:
            if self._nonce == -1:
                raise RuntimeError("NonceManager not initialized")

            nonce = self._nonce
            self._nonce += 1

        logger.debug("nonce_manager.dispensed", nonce=nonce)
        return nonce

    async def sync(self) -> None:
        """Re-fetch nonce from chain. Call after a tx revert or RPC error.

        In dry-run mode, skips the RPC call entirely.
        """
        if self._dry_run:
            logger.info("nonce_manager.sync_dry_run_skip")
            return

        async with self._lock:
            old_nonce = self._nonce

            try:
                self._nonce = await self._w3.eth.get_transaction_count(
                    self._address, "pending"
                )
            except Exception as exc:
                raise NonceManagerError(
                    f"Failed to sync nonce for {self._address}",
                    cause=exc,
                ) from exc

        logger.info(
            "nonce_manager.synced",
            old_nonce=old_nonce,
            new_nonce=self._nonce,
        )
