"""
tests/unit/test_config.py

Unit tests for AppConfig environment handling.
"""

from pydantic import SecretStr, ValidationError
import pytest
from web3 import Web3

from src.core.config import AppConfig

DRY_RUN_FALLBACK_RPC_URL = "https://rpc.ankr.com/polygon"
DRY_RUN_FALLBACK_WALLET_ADDRESS = "0x1111111111111111111111111111111111111111"
DRY_RUN_FALLBACK_PRIVATE_KEY = "0x" + "1" * 64


def _set_required_env(monkeypatch: pytest.MonkeyPatch, *, dry_run: bool) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-fake-key-000")
    monkeypatch.setenv("DRY_RUN", "true" if dry_run else "false")
    monkeypatch.delenv("POLYGON_RPC_URL", raising=False)
    monkeypatch.delenv("WALLET_ADDRESS", raising=False)
    monkeypatch.delenv("WALLET_PRIVATE_KEY", raising=False)


def test_app_config_enforces_exact_dry_run_fallbacks_when_env_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_required_env(monkeypatch, dry_run=True)

    with pytest.warns(UserWarning, match="DRY_RUN=True"):
        config = AppConfig(_env_file=None)

    assert config.dry_run is True
    assert config.polygon_rpc_url == DRY_RUN_FALLBACK_RPC_URL
    assert config.wallet_address == DRY_RUN_FALLBACK_WALLET_ADDRESS
    assert config.wallet_address != "0x0000000000000000000000000000000000000000"
    assert Web3.is_address(config.wallet_address)
    assert config.wallet_private_key == SecretStr(DRY_RUN_FALLBACK_PRIVATE_KEY)


def test_app_config_normalizes_dummy_polygon_rpc_url_to_exact_ankr_fallback_in_dry_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_required_env(monkeypatch, dry_run=True)
    monkeypatch.setenv("POLYGON_RPC_URL", "dummy_polygon_rpc")

    with pytest.warns(UserWarning, match="DRY_RUN=True"):
        config = AppConfig(_env_file=None)

    assert config.polygon_rpc_url == DRY_RUN_FALLBACK_RPC_URL
    assert config.wallet_address == DRY_RUN_FALLBACK_WALLET_ADDRESS
    assert config.wallet_private_key == SecretStr(DRY_RUN_FALLBACK_PRIVATE_KEY)


def test_app_config_requires_wallet_credentials_outside_dry_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_required_env(monkeypatch, dry_run=False)

    with pytest.raises(ValidationError):
        AppConfig(_env_file=None)
