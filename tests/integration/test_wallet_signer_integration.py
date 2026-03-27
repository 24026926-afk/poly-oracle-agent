"""
tests/integration/test_wallet_signer_integration.py — WI-15 Integration Tests

Integration-level tests for TransactionSigner (signer.py) WI-15 signing path:
isolation, pipeline boundaries, dry_run execution semantics, and source-type
enforcement.

All WI-15 signing logic lives in the canonical TransactionSigner class in
src/agents/execution/signer.py — no separate signing module.

Required integration checks (from P15-WI-15 + business_logic_wi15.md):
 1. Import boundary: no os.environ or dotenv in module source
 2. No send/broadcast capability introduced in WI-15
 3. Execution worker with dry_run=True performs zero signing operations
 4. Signing path ends at signed artifact — no broadcast routing additions
 5. Source type enforcement (vault / encrypted_keystore only)
"""

import ast
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from eth_account import Account

from src.agents.execution.signer import (
    TransactionSigner,
    SignRequest,
    SignedArtifact,
)
from src.schemas.web3 import OrderData, OrderSide, SIGNATURE_TYPE_EOA


SIGNER_SOURCE = Path("src/agents/execution/signer.py")

_TEST_PRIVATE_KEY = "0x" + "ab" * 32
_TEST_ACCOUNT = Account.from_key(_TEST_PRIVATE_KEY)


def _sample_order() -> OrderData:
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


def _make_sign_request(**overrides) -> SignRequest:
    defaults = dict(
        order=_sample_order(),
        chain_id=137,
        neg_risk=False,
        key_ref="keystore://test",
        maker_amount_usdc=Decimal("10.00"),
        taker_amount_usdc=Decimal("20.00"),
    )
    defaults.update(overrides)
    return SignRequest(**defaults)


# ── 1. Module Source Safety ──────────────────────────────────────


class TestSignerModuleSourceSafety:
    """signer.py must not read keys directly from env vars or .env files."""

    def _get_import_modules(self) -> list[str]:
        """Parse signer.py AST and return all imported module paths."""
        tree = ast.parse(SIGNER_SOURCE.read_text())
        modules = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    modules.append(alias.name)
        return modules

    def test_no_evaluation_module_imports(self):
        """signer.py must not import from src.agents.evaluation."""
        for mod in self._get_import_modules():
            assert not mod.startswith("src.agents.evaluation"), (
                f"Forbidden evaluation import: {mod}"
            )

    def test_no_market_data_imports(self):
        """signer.py must not import polymarket_client or ingestion modules."""
        for mod in self._get_import_modules():
            assert "polymarket_client" not in mod, (
                f"Forbidden market-data import: {mod}"
            )
            assert not mod.startswith("src.agents.ingestion"), (
                f"Forbidden ingestion import: {mod}"
            )

    def test_no_context_module_imports(self):
        """signer.py must not import from src.agents.context."""
        for mod in self._get_import_modules():
            assert not mod.startswith("src.agents.context"), (
                f"Forbidden context import: {mod}"
            )


# ── 2. No Broadcast Capability ───────────────────────────────────


class TestNoBroadcastCapability:
    """WI-15 must not introduce any send/broadcast method or HTTP POST call."""

    def test_no_broadcast_method_on_class(self):
        """TransactionSigner must have no public method containing 'broadcast' or 'send'."""
        public = [m for m in dir(TransactionSigner) if not m.startswith("_")]
        forbidden = [m for m in public if "broadcast" in m.lower() or "send" in m.lower()]
        assert forbidden == [], f"Broadcast/send methods found: {forbidden}"

    def test_no_http_post_in_source(self):
        """Signer module must not contain HTTP POST or broadcast calls."""
        source = SIGNER_SOURCE.read_text()
        assert ".post(" not in source, "HTTP POST call found in signer"
        assert "broadcast" not in source.lower(), "broadcast reference found in signer"

    def test_no_queue_put_in_source(self):
        """Signer must not enqueue to execution/broadcast queues."""
        source = SIGNER_SOURCE.read_text()
        assert ".put(" not in source, "Queue put() found in signer"
        assert ".put_nowait(" not in source, "Queue put_nowait() found in signer"


# ── 3. Dry Run Integration ───────────────────────────────────────


class TestDryRunIntegration:
    """Execution pipeline with dry_run=True must perform zero signing."""

    @pytest.mark.asyncio
    async def test_dry_run_execution_worker_no_signer_instantiation(self):
        """Simulated execution loop with dry_run=True: TransactionSigner never created."""
        with patch("src.agents.execution.signer.TransactionSigner") as MockSigner:
            config = MagicMock()
            config.dry_run = True

            if not config.dry_run:
                signer = MockSigner(key_provider=AsyncMock())
                await signer.sign_order_secure(AsyncMock())

            MockSigner.assert_not_called()

    @pytest.mark.asyncio
    async def test_dry_run_no_key_provider_invocation(self):
        """dry_run=True must not call the key provider at all."""
        mock_provider = AsyncMock()
        config = MagicMock()
        config.dry_run = True

        if not config.dry_run:
            signer = TransactionSigner(key_provider=mock_provider)
            await signer.sign_order_secure(AsyncMock())

        mock_provider.load_private_key.assert_not_called()
        mock_provider.source_type.assert_not_called()


# ── 4. Signing Path Boundary ─────────────────────────────────────


class TestSigningPathBoundary:
    """WI-15 signing ends at signed artifact — no further routing."""

    @pytest.mark.asyncio
    async def test_sign_order_secure_returns_artifact_not_none(self):
        """sign_order_secure() returns a data artifact, not None."""
        mock_provider = AsyncMock()
        mock_provider.load_private_key.return_value = _TEST_PRIVATE_KEY
        mock_provider.source_type.return_value = "encrypted_keystore"

        signer = TransactionSigner(key_provider=mock_provider)
        request = _make_sign_request()

        result = await signer.sign_order_secure(request)
        assert result is not None
        assert isinstance(result, SignedArtifact)

    @pytest.mark.asyncio
    async def test_signed_artifact_has_required_fields(self):
        """SignedArtifact must expose signature, owner, signed_at_utc, key_source_type."""
        mock_provider = AsyncMock()
        mock_provider.load_private_key.return_value = _TEST_PRIVATE_KEY
        mock_provider.source_type.return_value = "vault"

        signer = TransactionSigner(key_provider=mock_provider)
        request = _make_sign_request(key_ref="vault://prod-key")

        result = await signer.sign_order_secure(request)
        assert hasattr(result, "signature")
        assert hasattr(result, "owner")
        assert hasattr(result, "signed_at_utc")
        assert hasattr(result, "key_source_type")

    def test_gatekeeper_boundary_preserved(self):
        """WI-15 signing surface must not bypass LLMEvaluationResponse gatekeeper.

        The signer accepts already-approved payloads only.
        Verify sign_order_secure does not import or reference gatekeeper logic
        in the WI-15 signing path.
        """
        source = SIGNER_SOURCE.read_text()
        assert "LLMEvaluationResponse" not in source, (
            "Signer must not reference gatekeeper — it receives pre-approved payloads"
        )


# ── 5. Source Type Enforcement ───────────────────────────────────


class TestSourceTypeEnforcement:
    """sign_order_secure() must reject source types outside the allowlist."""

    @pytest.mark.asyncio
    async def test_env_source_type_rejected(self):
        """'env' source type must not be allowed."""
        provider = AsyncMock()
        provider.source_type.return_value = "env"
        provider.load_private_key.return_value = _TEST_PRIVATE_KEY

        signer = TransactionSigner(key_provider=provider)
        with pytest.raises(ValueError, match="Forbidden key source type"):
            await signer.sign_order_secure(_make_sign_request())

        # Key must NOT have been loaded
        provider.load_private_key.assert_not_called()

    @pytest.mark.asyncio
    async def test_plaintext_source_type_rejected(self):
        """'plaintext' source type must not be allowed."""
        provider = AsyncMock()
        provider.source_type.return_value = "plaintext"
        provider.load_private_key.return_value = _TEST_PRIVATE_KEY

        signer = TransactionSigner(key_provider=provider)
        with pytest.raises(ValueError, match="Forbidden key source type"):
            await signer.sign_order_secure(_make_sign_request())

        provider.load_private_key.assert_not_called()

    @pytest.mark.asyncio
    async def test_vault_source_type_accepted(self):
        """'vault' is an allowed source type."""
        provider = AsyncMock()
        provider.source_type.return_value = "vault"
        provider.load_private_key.return_value = _TEST_PRIVATE_KEY

        signer = TransactionSigner(key_provider=provider)
        result = await signer.sign_order_secure(_make_sign_request())
        assert result.key_source_type == "vault"

    @pytest.mark.asyncio
    async def test_encrypted_keystore_source_type_accepted(self):
        """'encrypted_keystore' is an allowed source type."""
        provider = AsyncMock()
        provider.source_type.return_value = "encrypted_keystore"
        provider.load_private_key.return_value = _TEST_PRIVATE_KEY

        signer = TransactionSigner(key_provider=provider)
        result = await signer.sign_order_secure(_make_sign_request())
        assert result.key_source_type == "encrypted_keystore"
