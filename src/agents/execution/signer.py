"""
src/agents/execution/signer.py

EIP-712 Typed Data Signer for Polymarket CTF Exchange orders.

Signs orders from first principles using ``eth_account`` — no dependency
on ``py-order-utils`` or ``py-clob-client``.  Domain and type definitions
match the on-chain CTF Exchange contract deployed on Polygon PoS.
"""

import secrets
from decimal import Decimal
from typing import Any, Dict

import structlog
from eth_account import Account
from eth_account.signers.local import LocalAccount

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


class TransactionSigner:
    """
    Constructs and signs Polymarket CLOB orders via EIP-712 typed data.

    The signer holds its own ``LocalAccount`` derived from the private key
    stored in ``AppConfig``.  All signing is local — no RPC call required.
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        raw_key: str = config.wallet_private_key.get_secret_value()
        self._account: LocalAccount = Account.from_key(raw_key)
        logger.info(
            "transaction_signer.initialised",
            address=self._account.address,
        )

    # -- public properties --------------------------------------------------

    @property
    def address(self) -> str:
        return self._account.address

    # -- signing ------------------------------------------------------------

    def sign_order(
        self,
        order: OrderData,
        neg_risk: bool = False,
    ) -> SignedOrder:
        """
        Produce an EIP-712 signature for *order*.

        Args:
            order: Validated ``OrderData`` with all fields populated.
            neg_risk: ``True`` to use the neg-risk exchange contract.

        Returns:
            ``SignedOrder`` containing the original order, hex signature,
            and the checksummed signer address.

        Raises:
            DryRunActiveError: If ``dry_run`` is enabled in config.
        """
        if self._config.dry_run:
            logger.info(
                "signer.dry_run_skip",
                dry_run=True,
                token_id=order.token_id,
                side=order.side.name,
            )
            raise DryRunActiveError("Order signing blocked: dry_run=True")

        domain = _build_eip712_domain(neg_risk)
        message = _order_to_message(order)

        signed = self._account.sign_typed_data(
            domain_data=domain,
            message_types=EIP712_ORDER_TYPES,
            message_data=message,
        )

        sig_hex: str = signed.signature.hex()
        # Ensure 0x prefix
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

    def build_order_from_decision(
        self,
        decision: Dict[str, Any],
        nonce: int = 0,
        fee_rate_bps: int = 0,
        neg_risk: bool = False,
    ) -> SignedOrder:
        """
        Map an approved agent decision into a signed ``OrderData``.

        ``decision`` is expected to carry:
        - ``evaluation`` — an ``LLMEvaluationResponse``
        - ``snapshot_id`` — for traceability (not encoded on-chain)

        A random 256-bit salt guarantees order uniqueness.

        Raises:
            DryRunActiveError: If ``dry_run`` is enabled in config.
        """
        if self._config.dry_run:
            logger.info(
                "signer.dry_run_skip",
                dry_run=True,
                method="build_order_from_decision",
            )
            raise DryRunActiveError(
                "Order construction blocked: dry_run=True"
            )

        eval_resp = decision["evaluation"]
        mc = eval_resp.market_context

        # Map schema action → on-chain side enum
        action = eval_resp.recommended_action.value
        side = OrderSide.BUY if action == "BUY" else OrderSide.SELL

        # Position size → USDC micro-units (6 decimals)
        # Why Decimal: float binary precision errors corrupt micro-unit calcs.
        bankroll_usdc = Decimal(str(decision.get("bankroll_usdc", "1000.0")))
        raw_usdc = Decimal(str(eval_resp.position_size_pct)) * bankroll_usdc
        maker_amount = int(raw_usdc * Decimal("1000000"))

        # Taker amount: tokens received at midpoint
        if mc.midpoint > 0:
            taker_amount = int(
                (raw_usdc / Decimal(str(mc.midpoint))) * Decimal("1000000")
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
