"""
src/schemas/web3.py

Pydantic V2 schemas for Web3 order construction and EIP-712 signing.
These models mirror the on-chain Order struct expected by the
Polymarket CTF Exchange smart contract.
"""

from enum import IntEnum

from pydantic import BaseModel, Field

from typing import Optional


class OrderSide(IntEnum):
    """On-chain side encoding: 0 = BUY, 1 = SELL."""

    BUY = 0
    SELL = 1



# Polymarket uses EOA = 0 for externally-owned accounts.
SIGNATURE_TYPE_EOA = 0


class OrderData(BaseModel):
    """
    Typed representation of the EIP-712 ``Order`` struct accepted by
    the Polymarket CTF Exchange.  All integer amounts are in the
    token's smallest unit (6 decimals for USDC).
    """

    salt: int = Field(..., description="Random nonce for uniqueness")
    maker: str = Field(..., description="Address funding the order")
    signer: str = Field(..., description="Address that signs the order")
    taker: str = Field(
        default="0x0000000000000000000000000000000000000000",
        description="Counterparty address (zero = any taker)",
    )
    token_id: int = Field(..., description="CTF ERC-1155 token ID (uint256)")
    maker_amount: int = Field(..., ge=0, description="Maker's contribution (USDC)")
    taker_amount: int = Field(..., ge=0, description="Taker's contribution (tokens)")
    expiration: int = Field(default=0, description="Unix expiration (0 = never)")
    nonce: int = Field(default=0, description="Exchange nonce for the maker")
    fee_rate_bps: int = Field(default=0, ge=0, description="Fee rate in basis points")
    side: OrderSide = Field(..., description="BUY (0) or SELL (1)")
    signature_type: int = Field(
        default=SIGNATURE_TYPE_EOA,
        description="Signature scheme (0 = EOA)",
    )

    model_config = {"frozen": True}


class SignedOrder(BaseModel):
    """Serialised order + hex signature, ready to POST to the CLOB."""

    order: OrderData
    signature: str = Field(
        ...,
        min_length=2,
        description="Hex-encoded EIP-712 signature (0x-prefixed)",
    )
    owner: str = Field(..., description="Checksummed signer address")

    def to_api_payload(self) -> dict:
        """Serialize for the Polymarket CLOB REST API.

        uint256 fields are converted to strings to prevent JavaScript
        precision loss on the server side.
        """
        o = self.order
        return {
            "order": {
                "salt": str(o.salt),
                "maker": o.maker,
                "signer": o.signer,
                "taker": o.taker,
                "tokenId": str(o.token_id),
                "makerAmount": str(o.maker_amount),
                "takerAmount": str(o.taker_amount),
                "expiration": str(o.expiration),
                "nonce": str(o.nonce),
                "feeRateBps": str(o.fee_rate_bps),
                "side": o.side.value,
                "signatureType": o.signature_type,
            },
            "signature": self.signature,
            "orderType": "GTC",
        }

    model_config = {"frozen": True}


class GasPrice(BaseModel):
    """EIP-1559 gas price estimate returned by GasEstimator."""

    base_fee_wei: int = Field(..., description="Base fee from latest block (Wei)")
    priority_fee_wei: int = Field(..., description="Priority tip with buffer (Wei)")
    max_fee_per_gas_wei: int = Field(..., description="maxFeePerGas for tx (Wei)")
    max_fee_per_gas_gwei: float = Field(..., description="maxFeePerGas in Gwei")
    is_fallback: bool = Field(
        default=False,
        description="True if RPC failed and fallback price was used",
    )

    model_config = {"frozen": True}


class TxReceiptSchema(BaseModel):
    """Parsed on-chain transaction receipt returned by the broadcaster."""

    order_id: str = Field(..., description="CLOB order ID returned by REST API")
    tx_hash: Optional[str] = Field(
        default=None, description="Polygon tx hash (if confirmed)"
    )
    status: str = Field(..., description="CONFIRMED, FAILED, PENDING, or REVERTED")
    gas_used: Optional[int] = Field(default=None, description="Gas consumed (Wei)")
    block_number: Optional[int] = Field(default=None, description="Block number")

    model_config = {"frozen": True}
