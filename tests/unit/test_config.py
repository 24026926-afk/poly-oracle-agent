"""
tests/unit/test_config.py

Unit tests for AppConfig environment handling.
"""

from urllib.parse import urlparse

from pydantic import SecretStr, ValidationError
import pytest
from web3 import Web3

from src.core.config import AppConfig


def _set_required_env(monkeypatch: pytest.MonkeyPatch, *, dry_run: bool) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-fake-key-000")
    monkeypatch.setenv("POLYGON_RPC_URL", "http://localhost:8545")
    monkeypatch.setenv("DRY_RUN", "true" if dry_run else "false")
    monkeypatch.delenv("WALLET_ADDRESS", raising=False)
    monkeypatch.delenv("WALLET_PRIVATE_KEY", raising=False)


def _assert_valid_http_url(url: str) -> None:
    parsed = urlparse(url)
    assert parsed.scheme in {"http", "https"}
    assert parsed.netloc


def test_app_config_allows_missing_wallet_credentials_in_dry_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_required_env(monkeypatch, dry_run=True)

    with pytest.warns(UserWarning, match="DRY_RUN=True"):
        config = AppConfig(_env_file=None)

    assert config.dry_run is True
    assert Web3.is_address(config.wallet_address)
    assert config.wallet_private_key == SecretStr("0x" + "11" * 32)


def test_app_config_allows_missing_polygon_rpc_url_in_dry_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_required_env(monkeypatch, dry_run=True)
    monkeypatch.delenv("POLYGON_RPC_URL", raising=False)

    with pytest.warns(UserWarning, match="DRY_RUN=True"):
        config = AppConfig(_env_file=None)

    _assert_valid_http_url(config.polygon_rpc_url)


def test_app_config_normalizes_dummy_polygon_rpc_url_in_dry_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_required_env(monkeypatch, dry_run=True)
    monkeypatch.setenv("POLYGON_RPC_URL", "dummy_polygon_rpc")

    with pytest.warns(UserWarning, match="DRY_RUN=True"):
        config = AppConfig(_env_file=None)

    _assert_valid_http_url(config.polygon_rpc_url)


def test_app_config_requires_wallet_credentials_outside_dry_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_required_env(monkeypatch, dry_run=False)

    with pytest.raises(ValidationError):
        AppConfig(_env_file=None)
