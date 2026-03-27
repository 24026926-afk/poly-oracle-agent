"""
src/agents/execution/signer.py

EIP-712 Typed Data Signer for Polymarket CTF Exchange orders.

Signs orders from first principles using ``eth_account`` — no dependency
on ``py-order-utils`` or ``py-clob-client``.  Domain and type definitions
match the on-chain CTF Exchange contract deployed on Polygon PoS.

WI-15 additions: secure key-provider protocol, typed ``SignRequest`` /
``SignedArtifact`` contracts, source-type enforcement, and async
``sign_order(request)`` entry point.
"""

import secrets
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Literal, Protocol

import structlog
from eth_account import Account
from eth_account.signers.local import LocalAccount
from pydantic import BaseModel, Field, field_validator

from src.core.config import AppConfig
from src.core.exceptions import DryRunActiveError
from src.schemas.web3 import OrderData, OrderSide, SignedOrder, SIGNATURE_TYPE_EOA

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Polymarket CTF Exchange — EIP-712 constants
# ---------------------------------------------------------------------------
CHAIN_ID: int = 137  # Polygon PoS

# Standard exchange
EXCHANGE_ADDRESS: str = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"

# Neg-risk exchange (multi-outcome markets)
NEG_RISK_EXCHANGE_ADDRESS: str = "0xC5d563A36AE78145C45a50134d48A1215220f80a"

ZERO_ADDRESS: str = "0x0000000000000000000000000000000000000000"

# EIP-712 Order types — must mirror the on-chain struct exactly.
EIP712_ORDER_TYPES: Dict[str, list] = {
    "Order": [
        {"name": "salt", "type": "uint256"},
        {"name": "maker", "type": "address"},
        {"name": "signer", "type": "address"},
        {"name": "taker", "type": "address"},
        {"name": "tokenId", "type": "uint256"},
        {"name": "makerAmount", "type": "uint256"},
        {"name": "takerAmount", "type": "uint256"},
        {"name": "expiration", "type": "uint256"},
        {"name": "nonce", "type": "uint256"},
        {"name": "feeRateBps", "type": "uint256"},
        {"name": "side", "type": "uint8"},
        {"name": "signatureType", "type": "uint8"},
    ],
}

_VALID_SOURCE_TYPES = frozenset({"vault", "encrypted_keystore"})


def _build_eip712_domain(neg_risk: bool = False) -> Dict[str, Any]:
    """Return the EIP-712 domain separator dict for the CTF Exchange."""
    return {
        "name": "Polymarket CTF Exchange",
        "version": "1.0.0",
        "chainId": CHAIN_ID,
        "verifyingContract": (
            NEG_RISK_EXCHANGE_ADDRESS if neg_risk else EXCHANGE_ADDRESS
        ),
    }


def _order_to_message(order: OrderData) -> Dict[str, Any]:
    """Serialize an ``OrderData`` into the EIP-712 message dict."""
    return {
        "salt": order.salt,
        "maker": order.maker,
        "signer": order.signer,
        "taker": order.taker,
        "tokenId": order.token_id,
        "makerAmount": order.maker_amount,
        "takerAmount": order.taker_amount,
        "expiration": order.expiration,
        "nonce": order.nonce,
        "feeRateBps": order.fee_rate_bps,
        "side": int(order.side),
        "signatureType": order.signature_type,
    }


# ---------------------------------------------------------------------------
# WI-15: Key Provider Protocol
# ---------------------------------------------------------------------------
class KeyProvider(Protocol):
    """Secure key provider interface (vault or encrypted keystore only)."""

    async def load_private_key(self, key_ref: str) -> str: ...

    async def source_type(self) -> Literal["vault", "encrypted_keystore"]: ...


# ---------------------------------------------------------------------------
# WI-15: Data Contracts (Pydantic V2)
# ---------------------------------------------------------------------------
class SignRequest(BaseModel):
    """Typed unsigned signing request — validated at schema boundary."""

    order: OrderData = Field(..., description="Full canonical Polymarket order payload")
    chain_id: int = Field(default=CHAIN_ID, description="Must be 137 (Polygon PoS)")
    neg_risk: bool = Field(default=False, description="Use neg-risk exchange")
    key_ref: str = Field(..., description="Opaque vault/keystore secret reference")
    maker_amount_usdc: Decimal = Field(
        ..., gt=Decimal("0"), description="Maker USDC amount (must be positive)",
    )
    taker_amount_usdc: Decimal = Field(
        ..., gt=Decimal("0"), description="Taker USDC amount (must be positive)",
    )

    @field_validator("chain_id")
    @classmethod
    def _validate_polygon_chain(cls, v: int) -> int:
        if v != CHAIN_ID:
            raise ValueError(f"chain_id must be {CHAIN_ID} (Polygon), got {v}")
        return v

    @field_validator("maker_amount_usdc", "taker_amount_usdc", mode="before")
    @classmethod
    def _reject_float_amounts(cls, v: Any) -> Decimal:
        if isinstance(v, float):
            raise ValueError("Float amounts forbidden in signer path — use Decimal")
        return v

    model_config = {"frozen": True}


class SignedArtifact(BaseModel):
    """Typed signed output — signature and audit metadata only."""

    signature: str = Field(
        ..., min_length=2, description="Hex EIP-712 signature (0x-prefixed)",
    )
    owner: str = Field(..., description="Checksummed signer address")
    signed_at_utc: datetime = Field(..., description="UTC timestamp of signing")
    key_source_type: str = Field(
        ..., description="Key source: 'vault' or 'encrypted_keystore'",
    )

    model_config = {"frozen": True}


# ---------------------------------------------------------------------------
# TransactionSigner — canonical signer class
# ---------------------------------------------------------------------------
class TransactionSigner:
    """
    Constructs and signs Polymarket CLOB orders via EIP-712 typed data.

    Supports two construction modes:

    * **Legacy** — ``TransactionSigner(config=cfg)`` reads key from
      ``AppConfig`` eagerly.  Used by existing pipeline callers.
    * **WI-15 secure** — ``TransactionSigner(key_provider=provider)``
      defers key retrieval to the async ``sign_order()`` path.
    """

    def __init__(
        self,
        config: AppConfig | None = None,
        *,
        key_provider: KeyProvider | None = None,
        expected_address: str | None = None,
    ) -> None:
        self._config = config
        self._key_provider = key_provider
        self._expected_address = expected_address
        self._account: LocalAccount | None = None

        # Legacy mode: eagerly load key from config
        if config is not None and key_provider is None:
            raw_key: str = config.wallet_private_key.get_secret_value()
            self._account = Account.from_key(raw_key)
            logger.info(
                "transaction_signer.initialised",
                address=self._account.address,
            )

    # -- public properties --------------------------------------------------

    @property
    def address(self) -> str:
        if self._account is None:
            raise RuntimeError("Address unavailable in key-provider mode")
        return self._account.address

    # -- WI-15 async signing entry point ------------------------------------

    async def sign_order_secure(self, request: SignRequest) -> SignedArtifact:
        """
        Sign the full canonical order payload via EIP-712 for Polygon.

        Loads key just-in-time from the configured provider, validates
        source type, signs the order, and returns a typed artifact.
        No side effects — no transmission or state mutation.
        """
        if self._key_provider is None:
            raise RuntimeError("sign_order_secure() requires key_provider")

        # Check source type BEFORE loading key material
        source_type = await self._key_provider.source_type()
        if source_type not in _VALID_SOURCE_TYPES:
            raise ValueError(
                f"Forbidden key source type: {source_type!r} "
                f"(allowed: {sorted(_VALID_SOURCE_TYPES)})"
            )

        raw_key: str | None = None
        try:
            raw_key = await self._key_provider.load_private_key(request.key_ref)
        except Exception:
            logger.error(
                "signer.key_provider_failed",
                key_ref_hash=hash(request.key_ref),
            )
            raise

        try:
            account: LocalAccount = Account.from_key(raw_key)
        except Exception:
            logger.error("signer.key_derivation_failed")
            raise
        finally:
            raw_key = None  # noqa: F841

        # Address mismatch guard
        if (
            self._expected_address is not None
            and account.address.lower() != self._expected_address.lower()
        ):
            logger.error(
                "signer.address_mismatch",
                expected=self._expected_address,
                derived=account.address,
            )
            raise ValueError(
                f"Derived address {account.address} does not match "
                f"expected {self._expected_address}"
            )

        # Sign the full canonical order
        domain = _build_eip712_domain(request.neg_risk)
        message = _order_to_message(request.order)

        signed = account.sign_typed_data(
            domain_data=domain,
            message_types=EIP712_ORDER_TYPES,
            message_data=message,
        )

        sig_hex: str = signed.signature.hex()
        if not sig_hex.startswith("0x"):
            sig_hex = "0x" + sig_hex

        logger.info(
            "signer.signed",
            owner=account.address,
            key_source_type=source_type,
            sig_prefix=sig_hex[:10],
        )

        return SignedArtifact(
            signature=sig_hex,
            owner=account.address,
            signed_at_utc=datetime.now(timezone.utc),
            key_source_type=source_type,
        )

    # -- legacy signing -------------------------------------------------------

    def sign_order(
        self,
        order: OrderData,
        neg_risk: bool = False,
    ) -> SignedOrder:
        """Sync signing — used by ``build_order_from_decision`` and legacy callers."""
        if self._config is not None and self._config.dry_run:
            logger.info(
                "signer.dry_run_skip",
                dry_run=True,
                token_id=order.token_id,
                side=order.side.name,
            )
            raise DryRunActiveError("Order signing blocked: dry_run=True")

        if self._account is None:
            raise RuntimeError("Legacy signing requires config-based construction")

        domain = _build_eip712_domain(neg_risk)
        message = _order_to_message(order)

        signed = self._account.sign_typed_data(
            domain_data=domain,
            message_types=EIP712_ORDER_TYPES,
            message_data=message,
        )

        sig_hex: str = signed.signature.hex()
        if not sig_hex.startswith("0x"):
            sig_hex = "0x" + sig_hex

        logger.debug(
            "order.signed",
            token_id=order.token_id,
            side=order.side.name,
            sig_prefix=sig_hex[:10],
        )

        return SignedOrder(
            order=order,
            signature=sig_hex,
            owner=self._account.address,
        )

    # -- convenience builder ------------------------------------------------

    async def build_order_from_decision(
        self,
        decision: Dict[str, Any],
        nonce: int = 0,
        fee_rate_bps: int = 0,
        neg_risk: bool = False,
        bankroll_tracker: "BankrollPortfolioTracker | None" = None,
    ) -> SignedOrder:
        """
        Map an approved agent decision into a signed ``OrderData``.

        ``decision`` is expected to carry an evaluation response and a
        ``snapshot_id`` for traceability (not encoded on-chain).

        A random 256-bit salt guarantees order uniqueness.
        """
        if self._config is not None and self._config.dry_run:
            logger.info(
                "signer.dry_run_skip",
                dry_run=True,
                method="build_order_from_decision",
            )
            raise DryRunActiveError(
                "Order construction blocked: dry_run=True"
            )

        if bankroll_tracker is None:
            raise ValueError("BankrollPortfolioTracker is required")

        eval_resp = decision["evaluation"]
        mc = eval_resp.market_context

        # Map schema action → on-chain side enum
        action = eval_resp.recommended_action.value
        side = OrderSide.BUY if action == "BUY" else OrderSide.SELL

        # Position size via tracker (Decimal math, Quarter-Kelly + 3% cap)
        raw_usdc = await bankroll_tracker.compute_position_size(
            kelly_fraction_raw=Decimal(str(eval_resp.position_size_pct)),
            condition_id=str(mc.condition_id),
        )
        await bankroll_tracker.validate_trade(raw_usdc, str(mc.condition_id))

        # USDC → micro-units (6 decimals)
        maker_amount = int(raw_usdc * Decimal("1e6"))

        # Taker amount: tokens received at midpoint
        if mc.midpoint > 0:
            taker_amount = int(
                (raw_usdc / Decimal(str(mc.midpoint))) * Decimal("1e6")
            )
        else:
            taker_amount = 0

        order = OrderData(
            salt=secrets.randbits(256),
            maker=self._account.address,
            signer=self._account.address,
            taker=ZERO_ADDRESS,
            token_id=mc.condition_id,
            maker_amount=maker_amount,
            taker_amount=taker_amount,
            expiration=0,
            nonce=nonce,
            fee_rate_bps=fee_rate_bps,
            side=side,
            signature_type=SIGNATURE_TYPE_EOA,
        )

        return self.sign_order(order, neg_risk=neg_risk)
