"""
tests/unit/test_signer.py

Unit tests for EIP-712 order signing against the Polymarket CTF Exchange.
"""

from unittest.mock import MagicMock

import pytest
from eth_account import Account

from src.agents.execution.signer import (
    CHAIN_ID,
    EXCHANGE_ADDRESS,
    NEG_RISK_EXCHANGE_ADDRESS,
    TransactionSigner,
    _build_eip712_domain,
    _order_to_message,
)
from src.schemas.web3 import OrderData, OrderSide, SIGNATURE_TYPE_EOA


# Deterministic test key — DO NOT use in production.
TEST_PRIVATE_KEY = "0x" + "ab" * 32
TEST_ACCOUNT = Account.from_key(TEST_PRIVATE_KEY)


def _make_config() -> MagicMock:
    """Create a minimal AppConfig mock exposing wallet_private_key."""
    cfg = MagicMock()
    secret = MagicMock()
    secret.get_secret_value.return_value = TEST_PRIVATE_KEY
    cfg.wallet_private_key = secret
    return cfg


def _sample_order() -> OrderData:
    return OrderData(
        salt=12345,
        maker=TEST_ACCOUNT.address,
        signer=TEST_ACCOUNT.address,
        taker="0x0000000000000000000000000000000000000000",
        token_id=71321045649585302271083621547358078199379994963399676385373543929026897356791,
        maker_amount=50_000_000,
        taker_amount=100_000_000,
        expiration=0,
        nonce=0,
        fee_rate_bps=0,
        side=OrderSide.BUY,
        signature_type=SIGNATURE_TYPE_EOA,
    )


# -------------------------------------------------------------------
# Domain
# -------------------------------------------------------------------
class TestEIP712Domain:
    def test_standard_exchange_domain(self):
        domain = _build_eip712_domain(neg_risk=False)
        assert domain["name"] == "Polymarket CTF Exchange"
        assert domain["version"] == "1.0.0"
        assert domain["chainId"] == 137
        assert domain["verifyingContract"] == EXCHANGE_ADDRESS

    def test_neg_risk_exchange_domain(self):
        domain = _build_eip712_domain(neg_risk=True)
        assert domain["verifyingContract"] == NEG_RISK_EXCHANGE_ADDRESS


# -------------------------------------------------------------------
# Order struct serialisation
# -------------------------------------------------------------------
class TestOrderMessage:
    def test_message_field_names(self):
        order = _sample_order()
        msg = _order_to_message(order)

        expected_keys = {
            "salt", "maker", "signer", "taker", "tokenId",
            "makerAmount", "takerAmount", "expiration", "nonce",
            "feeRateBps", "side", "signatureType",
        }
        assert set(msg.keys()) == expected_keys

    def test_message_values(self):
        order = _sample_order()
        msg = _order_to_message(order)

        assert msg["salt"] == 12345
        assert msg["maker"] == TEST_ACCOUNT.address
        assert msg["makerAmount"] == 50_000_000
        assert msg["takerAmount"] == 100_000_000
        assert msg["side"] == 0  # BUY


# -------------------------------------------------------------------
# Signing
# -------------------------------------------------------------------
class TestTransactionSigner:
    def test_signer_address_matches_key(self):
        signer = TransactionSigner(_make_config())
        assert signer.address == TEST_ACCOUNT.address

    def test_sign_order_returns_valid_signature(self):
        signer = TransactionSigner(_make_config())
        order = _sample_order()

        signed = signer.sign_order(order, neg_risk=False)

        assert signed.owner == TEST_ACCOUNT.address
        assert signed.signature.startswith("0x")
        assert len(signed.signature) > 2
        assert signed.order == order

    def test_sign_order_deterministic(self):
        """Same key + same order → identical signature."""
        signer = TransactionSigner(_make_config())
        order = _sample_order()

        sig_a = signer.sign_order(order, neg_risk=False).signature
        sig_b = signer.sign_order(order, neg_risk=False).signature

        assert sig_a == sig_b

    def test_sign_order_neg_risk_differs(self):
        """Different domain (neg_risk) → different signature."""
        signer = TransactionSigner(_make_config())
        order = _sample_order()

        sig_std = signer.sign_order(order, neg_risk=False).signature
        sig_neg = signer.sign_order(order, neg_risk=True).signature

        assert sig_std != sig_neg

    def test_chain_id_is_polygon(self):
        assert CHAIN_ID == 137
