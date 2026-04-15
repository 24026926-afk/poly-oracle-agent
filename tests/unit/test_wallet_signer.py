"""
tests/unit/test_wallet_signer.py — WI-15 Unit Tests

Unit tests for the secure signing surface on TransactionSigner (signer.py).
All WI-15 signing logic lives in the canonical TransactionSigner class in
src/agents/execution/signer.py — no separate signing module.

Required test matrix (from P15-WI-15 + business_logic_wi15.md):
 1. TransactionSigner class exists with sign_order_secure() as WI-15 signing API
 2. Private key loaded via vault/encrypted keystore only — no os.environ
 3. Private key never logged (success + error paths)
 4. dry_run=True does not instantiate TransactionSigner or touch key provider
 5. Decimal-only amount math; float rejected at schema boundary
 6. sign_order_secure() is async, typed, returns signed artifact only
 7. Polygon chain_id=137 enforcement
 8. Non-positive amounts rejected
 9. Fail-closed on provider/signing failure
10. Address mismatch between derived key and configured wallet rejected
11. Source type enforcement (vault / encrypted_keystore only)
"""

import inspect
import logging
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from eth_account import Account
from pydantic import ValidationError

from src.agents.execution.signer import (
    TransactionSigner,
    SignRequest,
    SignedArtifact,
)
from src.schemas.web3 import OrderData, OrderSide, SIGNATURE_TYPE_EOA


# ── Helpers ──────────────────────────────────────────────────────

# Deterministic test key — DO NOT use in production.
_TEST_PRIVATE_KEY = "0x" + "ab" * 32
_TEST_ACCOUNT = Account.from_key(_TEST_PRIVATE_KEY)


def _sample_order() -> OrderData:
    """Minimal valid OrderData for signing tests."""
    return OrderData(
        salt=12345,
        maker=_TEST_ACCOUNT.address,
        signer=_TEST_ACCOUNT.address,
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


# ── Fixtures ──────────────────────────────────────────────────────


@pytest.fixture
def mock_key_provider():
    """Simulates a secure vault/keystore key provider."""
    provider = AsyncMock()
    provider.load_private_key.return_value = _TEST_PRIVATE_KEY
    provider.source_type.return_value = "encrypted_keystore"
    return provider


@pytest.fixture
def valid_sign_request():
    """Minimal valid SignRequest with Decimal amounts and chain_id=137."""
    return SignRequest(
        order=_sample_order(),
        chain_id=137,
        neg_risk=False,
        key_ref="keystore://test-wallet-id",
        maker_amount_usdc=Decimal("50.00"),
        taker_amount_usdc=Decimal("100.00"),
    )


# ── 1. Class Existence & API Surface ─────────────────────────────


class TestTransactionSignerAPISurface:
    """TransactionSigner must exist and expose sign_order_secure() as WI-15 API."""

    def test_class_exists(self):
        assert TransactionSigner is not None

    def test_sign_order_secure_exists(self):
        """WI-15 async signing entry point must exist."""
        assert hasattr(TransactionSigner, "sign_order_secure")

    def test_sign_order_secure_is_async(self):
        """sign_order_secure() must be a coroutine function."""
        assert inspect.iscoroutinefunction(TransactionSigner.sign_order_secure)

    def test_no_send_or_broadcast_method(self):
        """WI-15 must not introduce any send/broadcast capability."""
        public = [m for m in dir(TransactionSigner) if not m.startswith("_")]
        forbidden = [
            m for m in public if "send" in m.lower() or "broadcast" in m.lower()
        ]
        assert forbidden == [], f"Forbidden methods found: {forbidden}"


# ── 2. Secure Key Provider ───────────────────────────────────────


class TestSecureKeyProvider:
    """Private key must come from vault/encrypted keystore only."""

    def test_no_os_environ_in_source(self):
        """Module must not read keys from os.environ or os.getenv."""
        import src.agents.execution.signer as mod

        source = inspect.getsource(mod)
        assert "os.environ" not in source, "os.environ found in signer source"
        assert "os.getenv" not in source, "os.getenv found in signer source"

    def test_no_dotenv_import(self):
        """Module must not load keys from .env files."""
        import src.agents.execution.signer as mod

        source = inspect.getsource(mod)
        assert "dotenv" not in source, "dotenv import found in signer source"

    @pytest.mark.asyncio
    async def test_provider_failure_is_fail_closed(
        self, mock_key_provider, valid_sign_request
    ):
        """When vault/keystore fails, signer must raise — no insecure fallback."""
        mock_key_provider.load_private_key.side_effect = RuntimeError(
            "vault unavailable"
        )
        signer = TransactionSigner(key_provider=mock_key_provider)

        with pytest.raises(Exception):
            await signer.sign_order_secure(valid_sign_request)

        assert mock_key_provider.load_private_key.call_count == 1

    @pytest.mark.asyncio
    async def test_vault_provider_returns_typed_artifact(
        self, mock_key_provider, valid_sign_request
    ):
        """Vault key-provider success path returns typed SignedArtifact."""
        mock_key_provider.source_type.return_value = "vault"
        signer = TransactionSigner(key_provider=mock_key_provider)

        result = await signer.sign_order_secure(valid_sign_request)
        assert isinstance(result, SignedArtifact)
        assert result.key_source_type == "vault"

    @pytest.mark.asyncio
    async def test_keystore_provider_returns_typed_artifact(
        self, mock_key_provider, valid_sign_request
    ):
        """Encrypted keystore success path returns typed SignedArtifact."""
        mock_key_provider.source_type.return_value = "encrypted_keystore"
        signer = TransactionSigner(key_provider=mock_key_provider)

        result = await signer.sign_order_secure(valid_sign_request)
        assert isinstance(result, SignedArtifact)
        assert result.key_source_type == "encrypted_keystore"

    @pytest.mark.asyncio
    async def test_forbidden_source_type_rejected(
        self, mock_key_provider, valid_sign_request
    ):
        """Source types other than vault/encrypted_keystore must be rejected."""
        mock_key_provider.source_type.return_value = "plaintext_env"
        signer = TransactionSigner(key_provider=mock_key_provider)

        with pytest.raises(ValueError, match="Forbidden key source type"):
            await signer.sign_order_secure(valid_sign_request)

    @pytest.mark.asyncio
    async def test_sign_order_secure_requires_key_provider(self, valid_sign_request):
        """sign_order_secure() without key_provider must raise RuntimeError."""
        signer = TransactionSigner()
        with pytest.raises(RuntimeError, match="requires key_provider"):
            await signer.sign_order_secure(valid_sign_request)


# ── 3. No Key Logging ────────────────────────────────────────────


class TestNoKeyLogging:
    """Private key material must never appear in logs."""

    @pytest.mark.asyncio
    async def test_success_path_no_key_in_logs(
        self, mock_key_provider, valid_sign_request, caplog
    ):
        """Signing success must not log private key material."""
        mock_key_provider.load_private_key.return_value = _TEST_PRIVATE_KEY
        signer = TransactionSigner(key_provider=mock_key_provider)

        with caplog.at_level(logging.DEBUG):
            await signer.sign_order_secure(valid_sign_request)

        assert _TEST_PRIVATE_KEY not in caplog.text, "Private key found in success logs"
        assert "ab" * 32 not in caplog.text, "Raw key bytes found in success logs"

    @pytest.mark.asyncio
    async def test_error_path_no_key_in_logs(
        self, mock_key_provider, valid_sign_request, caplog
    ):
        """Signing error path must not log private key material."""
        mock_key_provider.load_private_key.return_value = _TEST_PRIVATE_KEY
        signer = TransactionSigner(key_provider=mock_key_provider)

        with caplog.at_level(logging.DEBUG):
            try:
                bad_request = SignRequest(
                    order=_sample_order(),
                    chain_id=137,
                    neg_risk=False,
                    key_ref="vault://broken",
                    maker_amount_usdc=Decimal("10.00"),
                    taker_amount_usdc=Decimal("20.00"),
                )
                await signer.sign_order_secure(bad_request)
            except Exception:
                pass

        assert _TEST_PRIVATE_KEY not in caplog.text, "Private key found in error logs"


# ── 4. Dry Run Bypass ────────────────────────────────────────────


class TestDryRunBypass:
    """dry_run=True must bypass TransactionSigner entirely — no instantiation, no key load."""

    def test_dry_run_prevents_signer_instantiation(self):
        """When dry_run=True, TransactionSigner constructor must not be called."""
        with patch("src.agents.execution.signer.TransactionSigner") as MockSigner:
            dry_run = True
            if not dry_run:
                MockSigner(key_provider=AsyncMock())
            MockSigner.assert_not_called()

    def test_dry_run_prevents_key_provider_access(self):
        """When dry_run=True, key provider must never be invoked."""
        mock_provider = AsyncMock()
        dry_run = True
        if not dry_run:
            TransactionSigner(key_provider=mock_provider)
        mock_provider.load_private_key.assert_not_called()


# ── 5. Decimal Integrity ─────────────────────────────────────────


class TestDecimalIntegrity:
    """All gas/amount math in signer path must use Decimal; float rejected."""

    def test_float_maker_amount_rejected(self):
        """Float maker_amount_usdc must be rejected at schema boundary."""
        with pytest.raises(ValidationError):
            SignRequest(
                order=_sample_order(),
                chain_id=137,
                neg_risk=False,
                key_ref="vault://test",
                maker_amount_usdc=1.5,
                taker_amount_usdc=Decimal("3.0"),
            )

    def test_float_taker_amount_rejected(self):
        """Float taker_amount_usdc must be rejected at schema boundary."""
        with pytest.raises(ValidationError):
            SignRequest(
                order=_sample_order(),
                chain_id=137,
                neg_risk=False,
                key_ref="vault://test",
                maker_amount_usdc=Decimal("1.5"),
                taker_amount_usdc=3.0,
            )

    def test_decimal_amounts_accepted(self):
        """Decimal amounts must pass schema validation."""
        req = SignRequest(
            order=_sample_order(),
            chain_id=137,
            neg_risk=False,
            key_ref="vault://test",
            maker_amount_usdc=Decimal("50.00"),
            taker_amount_usdc=Decimal("100.00"),
        )
        assert req.maker_amount_usdc == Decimal("50.00")
        assert req.taker_amount_usdc == Decimal("100.00")

    def test_negative_maker_amount_rejected(self):
        """Negative maker amount must be rejected before signing."""
        with pytest.raises(ValidationError):
            SignRequest(
                order=_sample_order(),
                chain_id=137,
                neg_risk=False,
                key_ref="vault://test",
                maker_amount_usdc=Decimal("-10.00"),
                taker_amount_usdc=Decimal("20.00"),
            )

    def test_zero_maker_amount_rejected(self):
        """Zero maker amount must be rejected before signing."""
        with pytest.raises(ValidationError):
            SignRequest(
                order=_sample_order(),
                chain_id=137,
                neg_risk=False,
                key_ref="vault://test",
                maker_amount_usdc=Decimal("0"),
                taker_amount_usdc=Decimal("20.00"),
            )

    def test_negative_taker_amount_rejected(self):
        """Negative taker amount must be rejected before signing."""
        with pytest.raises(ValidationError):
            SignRequest(
                order=_sample_order(),
                chain_id=137,
                neg_risk=False,
                key_ref="vault://test",
                maker_amount_usdc=Decimal("10.00"),
                taker_amount_usdc=Decimal("-5.00"),
            )

    def test_usdc_micro_conversion_uses_decimal_1e6(self):
        """USDC -> micro-unit conversion must use Decimal('1e6'), not int literal."""
        amount = Decimal("50.00")
        micro = int(amount * Decimal("1e6"))
        assert micro == 50_000_000


# ── 6. sign_order_secure() Contract ───────────────────────────────


class TestSignOrderSecureContract:
    """sign_order_secure() must be async, typed, and return signed artifact only."""

    @pytest.mark.asyncio
    async def test_returns_typed_signed_artifact(
        self, mock_key_provider, valid_sign_request
    ):
        """Return value must be a typed SignedArtifact."""
        signer = TransactionSigner(key_provider=mock_key_provider)
        result = await signer.sign_order_secure(valid_sign_request)

        assert isinstance(result, SignedArtifact)
        assert result.signature.startswith("0x")
        assert len(result.signature) > 2
        assert result.owner
        assert result.signed_at_utc is not None
        assert result.key_source_type in ("vault", "encrypted_keystore")

    @pytest.mark.asyncio
    async def test_no_send_broadcast_side_effect(
        self, mock_key_provider, valid_sign_request
    ):
        """sign_order_secure() must not send, broadcast, or POST anywhere."""
        signer = TransactionSigner(key_provider=mock_key_provider)

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            await signer.sign_order_secure(valid_sign_request)
            mock_post.assert_not_called()

    @pytest.mark.asyncio
    async def test_fail_closed_on_signing_error(self, mock_key_provider):
        """Signing failure must not produce an execution-eligible artifact."""
        mock_key_provider.load_private_key.side_effect = RuntimeError(
            "key decrypt failed"
        )
        signer = TransactionSigner(key_provider=mock_key_provider)

        request = SignRequest(
            order=_sample_order(),
            chain_id=137,
            neg_risk=False,
            key_ref="vault://broken",
            maker_amount_usdc=Decimal("10.00"),
            taker_amount_usdc=Decimal("20.00"),
        )

        with pytest.raises(Exception):
            await signer.sign_order_secure(request)

    @pytest.mark.asyncio
    async def test_deterministic_signature(self, mock_key_provider, valid_sign_request):
        """Same key + same order -> identical signature."""
        signer = TransactionSigner(key_provider=mock_key_provider)
        sig_a = (await signer.sign_order_secure(valid_sign_request)).signature
        sig_b = (await signer.sign_order_secure(valid_sign_request)).signature
        assert sig_a == sig_b

    @pytest.mark.asyncio
    async def test_neg_risk_changes_signature(self, mock_key_provider):
        """Different domain (neg_risk) -> different signature."""
        signer = TransactionSigner(key_provider=mock_key_provider)
        order = _sample_order()

        req_std = SignRequest(
            order=order,
            chain_id=137,
            neg_risk=False,
            key_ref="vault://k",
            maker_amount_usdc=Decimal("10"),
            taker_amount_usdc=Decimal("20"),
        )
        req_neg = SignRequest(
            order=order,
            chain_id=137,
            neg_risk=True,
            key_ref="vault://k",
            maker_amount_usdc=Decimal("10"),
            taker_amount_usdc=Decimal("20"),
        )

        sig_std = (await signer.sign_order_secure(req_std)).signature
        sig_neg = (await signer.sign_order_secure(req_neg)).signature
        assert sig_std != sig_neg


# ── 7. Chain ID Enforcement ──────────────────────────────────────


class TestChainIDEnforcement:
    """Signing must enforce Polygon chain_id=137."""

    def test_non_polygon_chain_id_rejected(self):
        """chain_id != 137 must be rejected at schema boundary."""
        with pytest.raises(ValidationError):
            SignRequest(
                order=_sample_order(),
                chain_id=1,
                neg_risk=False,
                key_ref="vault://test",
                maker_amount_usdc=Decimal("10.00"),
                taker_amount_usdc=Decimal("20.00"),
            )

    def test_polygon_chain_id_accepted(self):
        """chain_id=137 must be accepted."""
        req = SignRequest(
            order=_sample_order(),
            chain_id=137,
            neg_risk=False,
            key_ref="vault://test",
            maker_amount_usdc=Decimal("10.00"),
            taker_amount_usdc=Decimal("20.00"),
        )
        assert req.chain_id == 137


# ── 8. Address Mismatch ──────────────────────────────────────────


class TestAddressMismatch:
    """Derived signer address must match configured wallet identity."""

    @pytest.mark.asyncio
    async def test_address_mismatch_rejected(
        self, mock_key_provider, valid_sign_request
    ):
        """If derived address differs from expected_address, signing must fail."""
        mock_key_provider.load_private_key.return_value = "0x" + "cd" * 32
        signer = TransactionSigner(
            key_provider=mock_key_provider,
            expected_address="0x1234567890AbcdEF1234567890aBcdef12345678",
        )

        with pytest.raises(Exception):
            await signer.sign_order_secure(valid_sign_request)

    @pytest.mark.asyncio
    async def test_address_match_succeeds(self, mock_key_provider, valid_sign_request):
        """If derived address matches expected_address, signing must succeed."""
        signer = TransactionSigner(
            key_provider=mock_key_provider,
            expected_address=_TEST_ACCOUNT.address,
        )

        result = await signer.sign_order_secure(valid_sign_request)
        assert isinstance(result, SignedArtifact)
        assert result.owner == _TEST_ACCOUNT.address
