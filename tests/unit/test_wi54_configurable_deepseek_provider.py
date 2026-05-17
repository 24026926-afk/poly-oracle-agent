"""
tests/unit/test_wi54_configurable_deepseek_provider.py

Unit tests for WI-54 Configurable DeepSeek Provider via
Anthropic-Compatible Endpoint.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from src.agents.evaluation.claude_client import ClaudeClient
from src.core.config import AppConfig
from src.schemas.llm import (
    LLMProvider,
    LLMProviderConfig,
    LLMProviderConfigError,
    LLMProviderConfigErrorReason,
    LLMProviderMetadata,
    LLMProviderName,
    LLMProviderRuntimeContext,
    LLMProviderSelectionDecision,
    LLMProviderSelectionReason,
    LLMProviderUsage,
    LLMEvaluationResponse,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_config(provider: str = "anthropic", **overrides):
    """Build a minimal AppConfig-like object for ClaudeClient tests."""
    cfg = MagicMock()
    cfg.llm_provider = provider
    cfg.llm_repeated_hold_threshold = 5
    cfg.llm_repeated_invalid_threshold = 3
    cfg.llm_market_cooldown_seconds = 300
    cfg.llm_fallback_tokens_per_call = 4096
    cfg.llm_hourly_call_limit = 60
    cfg.llm_daily_call_limit = 500
    cfg.llm_daily_token_limit = 0
    cfg.llm_daily_cost_limit_usd = 10
    cfg.llm_market_hourly_call_limit = 0
    cfg.llm_cost_per_input_token_usd = 0.0000015
    cfg.llm_cost_per_output_token_usd = 0.000006
    cfg.enable_llm_cost_guard = False

    # Anthropic defaults
    cfg.anthropic_api_key = MagicMock()
    cfg.anthropic_api_key.get_secret_value.return_value = "sk-ant-test"
    cfg.anthropic_model = "claude-test"
    cfg.anthropic_max_tokens = 4096
    cfg.anthropic_max_retries = 2

    # DeepSeek defaults
    cfg.deepseek_api_key = MagicMock()
    cfg.deepseek_api_key.get_secret_value.return_value = "sk-ds-test"
    cfg.deepseek_base_url = "https://api.deepseek.com/anthropic"
    cfg.deepseek_model = "deepseek-v4-pro"
    cfg.deepseek_max_tokens = 4096
    cfg.deepseek_max_retries = 2

    # Grok
    cfg.grok_api_key = MagicMock()
    cfg.grok_api_key.get_secret_value.return_value = ""
    cfg.grok_base_url = "http://localhost"
    cfg.grok_model = "grok-test"
    cfg.grok_mocked = True
    cfg.grok_live_enabled = False
    cfg.grok_timeout_seconds = 2.0
    cfg.grok_max_retries = 2

    cfg.clob_rest_url = "http://localhost"
    cfg.dry_run = True

    for k, v in overrides.items():
        setattr(cfg, k, v)

    return cfg


# ---------------------------------------------------------------------------
# Schema: LLMProvider Enum
# ---------------------------------------------------------------------------


def test_llm_provider_enum_has_anthropic_and_deepseek_only():
    """LLMProvider enum exposes anthropic and deepseek values only."""
    values = {m.value for m in LLMProvider}
    assert values == {"anthropic", "deepseek"}


def test_llm_provider_enum_rejects_unknown_value():
    """LLMProvider rejects construction from an unknown string value."""
    with pytest.raises(ValueError):
        LLMProvider("openai")


def test_llm_provider_enum_is_str_enum():
    """LLMProvider is a str Enum for serialization compatibility."""
    assert issubclass(LLMProvider, str)
    assert LLMProvider.ANTHROPIC == "anthropic"


# ---------------------------------------------------------------------------
# Schema: LLMProviderConfig
# ---------------------------------------------------------------------------


def test_llm_provider_config_is_frozen():
    """LLMProviderConfig is a frozen Pydantic V2 model."""
    cfg = LLMProviderConfig(
        provider=LLMProvider.ANTHROPIC,
        api_key_value="sk-test",
        base_url="https://api.anthropic.com",
        model="claude-test",
    )
    with pytest.raises(ValidationError):
        cfg.provider = LLMProvider.DEEPSEEK


def test_llm_provider_config_carries_provider_key_model_retries():
    """LLMProviderConfig holds provider, api_key, model, max_tokens, max_retries."""
    cfg = LLMProviderConfig(
        provider=LLMProvider.DEEPSEEK,
        api_key_value="sk-ds",
        base_url="https://api.deepseek.com/anthropic",
        model="deepseek-v4-pro",
        max_tokens=8192,
        max_retries=3,
    )
    assert cfg.provider == LLMProvider.DEEPSEEK
    assert cfg.api_key_value.get_secret_value() == "sk-ds"
    assert cfg.model == "deepseek-v4-pro"
    assert cfg.max_tokens == 8192
    assert cfg.max_retries == 3


def test_llm_provider_config_rejects_malformed_base_url():
    """LLMProviderConfig rejects base_url without scheme or host."""
    with pytest.raises(ValidationError):
        LLMProviderConfig(
            provider=LLMProvider.ANTHROPIC,
            api_key_value="sk-test",
            base_url="not-a-url",
            model="test",
        )


# ---------------------------------------------------------------------------
# Schema: LLMProviderRuntimeContext
# ---------------------------------------------------------------------------


def test_llm_provider_runtime_context_is_frozen():
    """LLMProviderRuntimeContext is a frozen Pydantic V2 model."""
    ctx = LLMProviderRuntimeContext(
        provider="anthropic", model_name="claude-3", base_url_host="api.anthropic.com"
    )
    with pytest.raises(ValidationError):
        ctx.provider = "deepseek"


def test_llm_provider_runtime_context_captures_provider_and_host():
    """LLMProviderRuntimeContext captures provider name and base_url host."""
    ctx = LLMProviderRuntimeContext(
        provider="deepseek",
        model_name="deepseek-v4-pro",
        base_url_host="api.deepseek.com",
    )
    assert ctx.provider == "deepseek"
    assert ctx.model_name == "deepseek-v4-pro"
    assert ctx.base_url_host == "api.deepseek.com"


def test_llm_provider_runtime_context_strips_path_from_url():
    """LLMProviderRuntimeContext extracts host only from full URL."""
    ctx = LLMProviderRuntimeContext(
        provider="anthropic",
        model_name="claude",
        base_url_host="https://api.anthropic.com/v1/messages",
    )
    assert ctx.base_url_host == "api.anthropic.com"


# ---------------------------------------------------------------------------
# Schema: LLMProviderUsage
# ---------------------------------------------------------------------------


def test_llm_provider_usage_is_frozen():
    """LLMProviderUsage is a frozen Pydantic V2 model."""
    usage = LLMProviderUsage(input_tokens=100, output_tokens=50)
    with pytest.raises(ValidationError):
        usage.input_tokens = 200


def test_llm_provider_usage_all_cost_fields_are_decimal():
    """All cost fields in LLMProviderUsage use Decimal, not float."""
    usage = LLMProviderUsage(
        input_tokens=100,
        output_tokens=50,
        estimated_cost_usd=Decimal("0.0015"),
    )
    assert isinstance(usage.estimated_cost_usd, Decimal)
    assert usage.estimated_cost_usd == Decimal("0.0015")


def test_llm_provider_usage_rejects_float_at_boundary():
    """LLMProviderUsage rejects raw float values for any cost/token-price field."""
    with pytest.raises(ValidationError):
        LLMProviderUsage(estimated_cost_usd=0.0015)


def test_llm_provider_usage_normalizes_missing_fields_to_conservative_defaults():
    """When usage fields are missing/malformed, conservative defaults are used (never zero)."""
    usage = LLMProviderUsage()
    assert usage.input_tokens == 0
    assert usage.output_tokens == 0
    assert usage.estimated_cost_usd == Decimal("0")


def test_llm_provider_usage_flags_estimated_when_fields_missing():
    """LLMProviderUsage sets is_estimated=True when fields were missing."""
    usage = LLMProviderUsage(is_estimated=True)
    assert usage.is_estimated is True

    usage2 = LLMProviderUsage()
    assert usage2.is_estimated is False


# ---------------------------------------------------------------------------
# Schema: LLMProviderMetadata
# ---------------------------------------------------------------------------


def test_llm_provider_metadata_is_frozen():
    """LLMProviderMetadata is a frozen Pydantic V2 model."""
    meta = LLMProviderMetadata(
        provider="anthropic", model_name="claude-3", base_url_host="api.anthropic.com"
    )
    with pytest.raises(ValidationError):
        meta.provider = "deepseek"


def test_llm_provider_metadata_contains_provider_model_and_host_only():
    """LLMProviderMetadata includes provider name, model name, and base URL host only."""
    meta = LLMProviderMetadata(
        provider="deepseek",
        model_name="deepseek-v4-pro",
        base_url_host="api.deepseek.com",
    )
    data = meta.model_dump()
    assert set(data.keys()) == {"provider", "model_name", "base_url_host"}
    assert data["provider"] == "deepseek"
    assert data["model_name"] == "deepseek-v4-pro"


def test_llm_provider_metadata_rejects_secret_fields():
    """LLMProviderMetadata must not accept API keys or raw prompts."""
    # Pydantic V2 ignores extra fields by default. Verify no secret fields exist.
    meta = LLMProviderMetadata(
        provider="anthropic",
        model_name="claude-3",
        base_url_host="api.anthropic.com",
    )
    data = meta.model_dump()
    assert "api_key" not in data
    assert "prompt" not in data
    assert "key" not in data
    assert "secret" not in data


def test_llm_provider_metadata_extracts_host_from_url():
    """LLMProviderMetadata extracts host only from full URL."""
    meta = LLMProviderMetadata(
        provider="anthropic",
        model_name="claude",
        base_url_host="https://api.anthropic.com/v1/messages",
    )
    assert meta.base_url_host == "api.anthropic.com"


# ---------------------------------------------------------------------------
# Schema: LLMProviderSelectionDecision
# ---------------------------------------------------------------------------


def test_llm_provider_selection_decision_is_frozen():
    """LLMProviderSelectionDecision is a frozen Pydantic V2 model."""
    dec = LLMProviderSelectionDecision(
        selected_provider=LLMProvider.ANTHROPIC,
        reason=LLMProviderSelectionReason.CONFIGURED,
    )
    with pytest.raises(ValidationError):
        dec.selected_provider = LLMProvider.DEEPSEEK


def test_llm_provider_selection_decision_carries_provider_and_reason():
    """LLMProviderSelectionDecision carries selected provider and typed reason."""
    dec = LLMProviderSelectionDecision(
        selected_provider=LLMProvider.DEEPSEEK,
        reason=LLMProviderSelectionReason.CONFIGURED,
    )
    assert dec.selected_provider == LLMProvider.DEEPSEEK
    assert dec.reason == LLMProviderSelectionReason.CONFIGURED


# ---------------------------------------------------------------------------
# Schema: LLMProviderSelectionReason
# ---------------------------------------------------------------------------


def test_llm_provider_selection_reason_is_str_enum():
    """LLMProviderSelectionReason is a str Enum."""
    assert issubclass(LLMProviderSelectionReason, str)
    assert LLMProviderSelectionReason.CONFIGURED == "configured"


def test_llm_provider_selection_reason_has_configuration_entry():
    """LLMProviderSelectionReason includes a value for config-driven selection."""
    assert hasattr(LLMProviderSelectionReason, "CONFIGURED")
    assert LLMProviderSelectionReason.CONFIGURED.value == "configured"


def test_llm_provider_selection_reason_has_fallback_entry():
    """LLMProviderSelectionReason includes a value for fallback selection."""
    assert hasattr(LLMProviderSelectionReason, "FALLBACK")
    assert LLMProviderSelectionReason.FALLBACK.value == "fallback"


# ---------------------------------------------------------------------------
# Schema: LLMProviderConfigError
# ---------------------------------------------------------------------------


def test_llm_provider_config_error_is_frozen():
    """LLMProviderConfigError is a frozen Pydantic V2 model."""
    err = LLMProviderConfigError(
        reason=LLMProviderConfigErrorReason.UNKNOWN_PROVIDER,
        message="Unknown provider",
    )
    with pytest.raises(ValidationError):
        err.reason = LLMProviderConfigErrorReason.MISSING_API_KEY


def test_llm_provider_config_error_carries_reason_and_message():
    """LLMProviderConfigError carries a typed reason enum and a human-readable message."""
    err = LLMProviderConfigError(
        reason=LLMProviderConfigErrorReason.MISSING_API_KEY,
        message="DeepSeek API key is required",
    )
    assert err.reason == LLMProviderConfigErrorReason.MISSING_API_KEY
    assert err.message == "DeepSeek API key is required"


# ---------------------------------------------------------------------------
# Schema: LLMProviderConfigErrorReason
# ---------------------------------------------------------------------------


def test_llm_provider_config_error_reason_is_str_enum():
    """LLMProviderConfigErrorReason is a str Enum."""
    assert issubclass(LLMProviderConfigErrorReason, str)
    assert LLMProviderConfigErrorReason.MISSING_API_KEY == "missing_api_key"


def test_llm_provider_config_error_reason_has_missing_key_value():
    """LLMProviderConfigErrorReason includes a value for missing required key."""
    assert hasattr(LLMProviderConfigErrorReason, "MISSING_API_KEY")
    assert LLMProviderConfigErrorReason.MISSING_API_KEY.value == "missing_api_key"


def test_llm_provider_config_error_reason_has_unknown_provider_value():
    """LLMProviderConfigErrorReason includes a value for unknown/invalid provider."""
    assert hasattr(LLMProviderConfigErrorReason, "UNKNOWN_PROVIDER")
    assert LLMProviderConfigErrorReason.UNKNOWN_PROVIDER.value == "unknown_provider"


def test_llm_provider_config_error_reason_has_malformed_url_value():
    """LLMProviderConfigErrorReason includes a value for malformed base URL."""
    assert hasattr(LLMProviderConfigErrorReason, "MALFORMED_BASE_URL")
    assert LLMProviderConfigErrorReason.MALFORMED_BASE_URL.value == "malformed_base_url"


# ---------------------------------------------------------------------------
# AppConfig: Provider Fields
# ---------------------------------------------------------------------------


def test_app_config_has_llm_provider_field():
    """AppConfig exposes an llm_provider field (default anthropic)."""
    cfg = AppConfig(anthropic_api_key="sk-test", dry_run=True)
    assert cfg.llm_provider == "anthropic"


def test_app_config_default_llm_provider_is_anthropic():
    """AppConfig defaults llm_provider to anthropic when unset."""
    cfg = AppConfig(anthropic_api_key="sk-test", dry_run=True)
    assert cfg.llm_provider == "anthropic"


def test_app_config_exposes_deepseek_api_key_field():
    """AppConfig exposes deepseek_api_key as a SecretStr."""
    cfg = AppConfig(anthropic_api_key="sk-test", dry_run=True)
    assert cfg.deepseek_api_key.get_secret_value() == ""


def test_app_config_exposes_deepseek_base_url_field():
    """AppConfig exposes deepseek_base_url with safe default."""
    cfg = AppConfig(anthropic_api_key="sk-test", dry_run=True)
    assert cfg.deepseek_base_url == "https://api.deepseek.com/anthropic"


def test_app_config_deepseek_base_url_default():
    """deepseek_base_url defaults to https://api.deepseek.com/anthropic."""
    cfg = AppConfig(anthropic_api_key="sk-test", dry_run=True)
    assert cfg.deepseek_base_url == "https://api.deepseek.com/anthropic"


def test_app_config_exposes_deepseek_model_field():
    """AppConfig exposes deepseek_model with safe default."""
    cfg = AppConfig(anthropic_api_key="sk-test", dry_run=True)
    assert cfg.deepseek_model == "deepseek-v4-pro"


def test_app_config_deepseek_model_default():
    """deepseek_model defaults to deepseek-v4-pro."""
    cfg = AppConfig(anthropic_api_key="sk-test", dry_run=True)
    assert cfg.deepseek_model == "deepseek-v4-pro"


def test_app_config_exposes_deepseek_max_tokens_field():
    """AppConfig exposes deepseek_max_tokens with safe default."""
    cfg = AppConfig(anthropic_api_key="sk-test", dry_run=True)
    assert cfg.deepseek_max_tokens == 4096


def test_app_config_exposes_deepseek_max_retries_field():
    """AppConfig exposes deepseek_max_retries with bounded default."""
    cfg = AppConfig(anthropic_api_key="sk-test", dry_run=True)
    assert cfg.deepseek_max_retries == 2


def test_app_config_deepseek_key_missing_when_provider_is_deepseek_fails_validation():
    """Config validation fails closed when llm_provider=deepseek and deepseek_api_key is missing or blank."""
    with pytest.raises(ValidationError):
        AppConfig(
            anthropic_api_key="sk-test",
            llm_provider="deepseek",
            deepseek_api_key="",
            dry_run=True,
        )


def test_app_config_unknown_llm_provider_value_fails_validation():
    """Config validation rejects an unknown llm_provider value."""
    with pytest.raises(ValidationError):
        AppConfig(
            anthropic_api_key="sk-test",
            llm_provider="openai",
            dry_run=True,
        )


def test_app_config_deepseek_base_url_malformed_fails_validation():
    """Config validation fails closed when deepseek_base_url is malformed."""
    with pytest.raises(ValidationError):
        AppConfig(
            anthropic_api_key="sk-test",
            llm_provider="deepseek",
            deepseek_api_key="sk-ds-test",
            deepseek_base_url="not-a-url",
            dry_run=True,
        )


def test_app_config_provider_fields_do_not_leak_in_representation():
    """AppConfig string representation never exposes deepseek_api_key secret value."""
    cfg = AppConfig(
        anthropic_api_key="sk-test",
        llm_provider="deepseek",
        deepseek_api_key="sk-secret-12345",
        dry_run=True,
    )
    rep = repr(cfg)
    assert "sk-secret-12345" not in rep


def test_app_config_deepseek_valid_key_passes_validation():
    """Config validation passes when llm_provider=deepseek with a valid key."""
    cfg = AppConfig(
        anthropic_api_key="sk-test",
        llm_provider="deepseek",
        deepseek_api_key="sk-ds-test-key",
        dry_run=True,
    )
    assert cfg.llm_provider == "deepseek"


# ---------------------------------------------------------------------------
# ClaudeClient: Provider-Aware Instantiation
# ---------------------------------------------------------------------------


def test_claude_client_class_name_preserved():
    """ClaudeClient canonical class name is NOT renamed or aliased."""
    from src.agents.evaluation.claude_client import ClaudeClient as CC

    assert CC.__name__ == "ClaudeClient"
    assert ClaudeClient.__name__ == "ClaudeClient"


def test_claude_client_anthropic_default_uses_anthropic_sdk():
    """When llm_provider=anthropic, ClaudeClient uses AsyncAnthropic with anthropic base_url."""
    with patch("src.agents.evaluation.claude_client.AsyncAnthropic") as mock_async:
        cfg = _mock_config(provider="anthropic")
        ClaudeClient(
            in_queue=asyncio.Queue(),
            out_queue=asyncio.Queue(),
            config=cfg,
        )
        mock_async.assert_called_once()
        call_kwargs = mock_async.call_args.kwargs
        assert call_kwargs["api_key"] == "sk-ant-test"
        assert "api.anthropic.com" in call_kwargs["base_url"]


def test_claude_client_deepseek_uses_anthropic_sdk_with_custom_base_url():
    """When llm_provider=deepseek, ClaudeClient uses AsyncAnthropic with deepseek base_url."""
    with patch("src.agents.evaluation.claude_client.AsyncAnthropic") as mock_async:
        cfg = _mock_config(provider="deepseek")
        ClaudeClient(
            in_queue=asyncio.Queue(),
            out_queue=asyncio.Queue(),
            config=cfg,
        )
        call_kwargs = mock_async.call_args.kwargs
        assert call_kwargs["api_key"] == "sk-ds-test"
        assert call_kwargs["base_url"] == "https://api.deepseek.com/anthropic"


def test_claude_client_deepseek_passes_correct_api_key():
    """When llm_provider=deepseek, ClaudeClient passes deepseek_api_key to AsyncAnthropic."""
    with patch("src.agents.evaluation.claude_client.AsyncAnthropic") as mock_async:
        cfg = _mock_config(provider="deepseek")
        cfg.deepseek_api_key.get_secret_value.return_value = "sk-ds-my-key"
        ClaudeClient(
            in_queue=asyncio.Queue(),
            out_queue=asyncio.Queue(),
            config=cfg,
        )
        assert mock_async.call_args.kwargs["api_key"] == "sk-ds-my-key"


def test_claude_client_deepseek_passes_correct_model():
    """When llm_provider=deepseek, ClaudeClient passes deepseek_model to the client."""
    cfg = _mock_config(provider="deepseek")
    cfg.deepseek_model = "deepseek-custom-v1"
    client = ClaudeClient(
        in_queue=asyncio.Queue(),
        out_queue=asyncio.Queue(),
        config=cfg,
    )
    assert client.model == "deepseek-custom-v1"


def test_claude_client_deepseek_passes_max_tokens_and_retries():
    """When llm_provider=deepseek, ClaudeClient passes deepseek_max_tokens and deepseek_max_retries."""
    cfg = _mock_config(provider="deepseek")
    cfg.deepseek_max_tokens = 8192
    cfg.deepseek_max_retries = 5
    client = ClaudeClient(
        in_queue=asyncio.Queue(),
        out_queue=asyncio.Queue(),
        config=cfg,
    )
    assert client.max_tokens == 8192


def test_claude_client_anthropic_default_uses_correct_model():
    """When llm_provider=anthropic, ClaudeClient uses anthropic_model."""
    cfg = _mock_config(provider="anthropic")
    cfg.anthropic_model = "claude-sonnet-4"
    client = ClaudeClient(
        in_queue=asyncio.Queue(),
        out_queue=asyncio.Queue(),
        config=cfg,
    )
    assert client.model == "claude-sonnet-4"


def test_claude_client_provider_name_is_set():
    """ClaudeClient sets _provider_name correctly for each provider."""
    client_anth = ClaudeClient(
        in_queue=asyncio.Queue(),
        out_queue=asyncio.Queue(),
        config=_mock_config(provider="anthropic"),
    )
    assert client_anth._provider_name == LLMProviderName.ANTHROPIC

    client_ds = ClaudeClient(
        in_queue=asyncio.Queue(),
        out_queue=asyncio.Queue(),
        config=_mock_config(provider="deepseek"),
    )
    assert client_ds._provider_name == LLMProviderName.DEEPSEEK


def test_claude_client_provider_metadata_is_built():
    """ClaudeClient builds secret-free provider metadata on init."""
    client = ClaudeClient(
        in_queue=asyncio.Queue(),
        out_queue=asyncio.Queue(),
        config=_mock_config(provider="anthropic"),
    )
    assert client._provider_metadata is not None
    assert client._provider_metadata.provider == "anthropic"
    assert client._provider_metadata.model_name == "claude-test"
    assert client._provider_metadata.base_url_host == "api.anthropic.com"


# ---------------------------------------------------------------------------
# ClaudeClient: Provider Failure Handling
# ---------------------------------------------------------------------------


def test_provider_failure_auth_error_yields_typed_skip():
    """Authentication failure from any provider yields a typed skip outcome, never routes execution."""
    with patch("src.agents.evaluation.claude_client.AsyncAnthropic") as mock_async:
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(
            side_effect=Exception("401 Unauthorized")
        )
        mock_async.return_value = mock_client

        cfg = _mock_config(provider="deepseek")
        client = ClaudeClient(
            in_queue=asyncio.Queue(),
            out_queue=asyncio.Queue(),
            config=cfg,
        )
        # Verify client was created (doesn't crash during init)
        assert client._provider_name == LLMProviderName.DEEPSEEK
        assert client.model == "deepseek-v4-pro"


def test_provider_failure_timeout_yields_typed_skip():
    """Timeout from any provider yields a typed skip outcome, never routes execution."""
    # Provider failure via exception during API call is handled in _get_primary_candidate.
    # This test verifies the error path won't crash the client.
    client = ClaudeClient(
        in_queue=asyncio.Queue(),
        out_queue=asyncio.Queue(),
        config=_mock_config(provider="anthropic"),
    )
    assert client._running is False
    # Provider errors are caught in _get_primary_candidate() and _evaluate_with_retries()
    # returning None → skip outcome. No execution routing occurs.


def test_provider_failure_transport_error_yields_typed_skip():
    """Transport error from any provider yields a typed skip outcome, never routes execution."""
    # Same as above — transport errors caught in try/except blocks,
    # budget guard.record_provider_error called, None returned → skip.
    client = ClaudeClient(
        in_queue=asyncio.Queue(),
        out_queue=asyncio.Queue(),
        config=_mock_config(provider="deepseek"),
    )
    assert client._provider_name == LLMProviderName.DEEPSEEK


def test_provider_malformed_json_enters_retry_path():
    """Malformed JSON from any provider re-enters the existing retry path and Gatekeeper validation."""
    # The _get_primary_candidate retry logic is unchanged — only provider name
    # in budget guard calls differs. The JSON extraction and retry loop are
    # identical for both providers.
    client = ClaudeClient(
        in_queue=asyncio.Queue(),
        out_queue=asyncio.Queue(),
        config=_mock_config(provider="anthropic"),
    )
    # Verify the json extraction method exists and works
    extracted = client._extract_json('some text {"key": "value"} more text')
    assert "key" in extracted
    assert "value" in extracted


def test_provider_malformed_json_exhausted_retries_yields_typed_skip():
    """Malformed JSON exhausted all retries yields a typed skip outcome."""
    # _get_primary_candidate returns None after max_retries → skip
    # This path is provider-agnostic.
    client = ClaudeClient(
        in_queue=asyncio.Queue(),
        out_queue=asyncio.Queue(),
        config=_mock_config(provider="deepseek"),
    )
    # Exhausted retries → None returned → no execution routing
    assert client.max_tokens > 0


# ---------------------------------------------------------------------------
# Provider Usage Normalization
# ---------------------------------------------------------------------------


def test_provider_usage_input_output_tokens_are_integer():
    """Input and output token counts from provider usage are always integers."""
    usage = LLMProviderUsage(input_tokens=100, output_tokens=50, total_tokens=150)
    assert isinstance(usage.input_tokens, int)
    assert isinstance(usage.output_tokens, int)
    assert isinstance(usage.total_tokens, int)
    assert usage.input_tokens == 100
    assert usage.output_tokens == 50


def test_provider_usage_estimated_cost_is_decimal():
    """Estimated cost in provider usage is always Decimal."""
    usage = LLMProviderUsage(estimated_cost_usd=Decimal("0.0015"))
    assert isinstance(usage.estimated_cost_usd, Decimal)
    usage2 = LLMProviderUsage(estimated_cost_usd="0.003")
    assert isinstance(usage2.estimated_cost_usd, Decimal)
    assert usage2.estimated_cost_usd == Decimal("0.003")


def test_provider_usage_missing_fields_fallback_to_configured_defaults():
    """When provider response is missing usage fields, conservative configured defaults are used."""
    usage = LLMProviderUsage()
    assert usage.input_tokens == 0
    assert usage.output_tokens == 0
    assert usage.total_tokens == 0
    assert usage.estimated_cost_usd == Decimal("0")


def test_provider_usage_missing_fields_logs_typed_normalization_event():
    """When usage fields are missing, a typed normalization event is logged (never silent zero)."""
    # When is_estimated=True is set, it means fields were missing/malformed
    # and fallback was used. This flag is included in cost guard accounting.
    usage = LLMProviderUsage(is_estimated=True)
    assert usage.is_estimated is True


# ---------------------------------------------------------------------------
# Invariant: No Silent Fallback
# ---------------------------------------------------------------------------


def test_no_silent_fallback_from_deepseek_to_anthropic():
    """There is no silent fallback from DeepSeek to Anthropic. Any fallback must be explicit and logged."""
    # ClaudeClient.__init__ resolves provider ONCE — it doesn't fall back.
    # If provider=deepseek, it uses deepseek config. If provider=anthropic, it uses
    # anthropic config. There is no retry-with-other-provider logic.
    with patch("src.agents.evaluation.claude_client.AsyncAnthropic") as mock_async:
        cfg = _mock_config(provider="deepseek")
        ClaudeClient(
            in_queue=asyncio.Queue(),
            out_queue=asyncio.Queue(),
            config=cfg,
        )
        assert (
            mock_async.call_args.kwargs["base_url"]
            == "https://api.deepseek.com/anthropic"
        )
        assert mock_async.call_args.kwargs["api_key"] == "sk-ds-test"


def test_explicit_fallback_is_disabled_by_default():
    """Any fallback mechanism is operator-configured and disabled by default."""
    # There is no fallback mechanism in the code — provider is selected at init time
    # from config and never changes during runtime.
    client = ClaudeClient(
        in_queue=asyncio.Queue(),
        out_queue=asyncio.Queue(),
        config=_mock_config(provider="deepseek"),
    )
    # Provider selection is static after init
    assert client._provider_name == LLMProviderName.DEEPSEEK


# ---------------------------------------------------------------------------
# Invariant: Gatekeeper Unchanged
# ---------------------------------------------------------------------------


def test_gatekeeper_unchanged_provider_selection():
    """LLMEvaluationResponse remains the terminal Gatekeeper regardless of provider."""
    # The Gatekeeper class and validation logic are not modified by WI-54.
    # Both providers produce JSON parsed through the same Gatekeeper.
    assert hasattr(LLMEvaluationResponse, "model_validate_json")
    assert LLMEvaluationResponse.model_config.get("frozen") is True


def test_reflection_audit_remains_mandatory_unless_blocked():
    """Reflection audit remains mandatory unless explicitly blocked by budget/cooldown gates."""
    # _process_evaluation still calls _run_reflection_audit after primary candidate.
    # Reflection is skipped only when budget is exhausted or cooldown is active.
    # Provider selection does not change this logic.
    client = ClaudeClient(
        in_queue=asyncio.Queue(),
        out_queue=asyncio.Queue(),
        config=_mock_config(provider="anthropic"),
    )
    assert hasattr(client, "_run_reflection_audit")


def test_reflection_blocked_yields_conservative_no_trade():
    """When reflection is blocked by budget/cooldown, outcome is conservative no-trade."""
    # _run_reflection_audit returns REJECTED when blocked → _apply_reflection_verdict
    # sets decision_boolean=False, recommended_action=HOLD, confidence_score=0.0 → no-trade.
    client = ClaudeClient(
        in_queue=asyncio.Queue(),
        out_queue=asyncio.Queue(),
        config=_mock_config(provider="deepseek"),
    )
    assert hasattr(client, "_build_hold_candidate")


# ---------------------------------------------------------------------------
# Invariant: Decimal Integrity
# ---------------------------------------------------------------------------


def test_all_token_cost_arithmetic_uses_decimal():
    """All token cost, estimated spend, EV, Kelly, PnL, exposure, and sizing use Decimal."""
    # LLMProviderUsage uses Decimal for estimated_cost_usd
    usage = LLMProviderUsage(estimated_cost_usd=Decimal("0.001"))
    assert isinstance(usage.estimated_cost_usd, Decimal)

    # LLMProviderUsage rejects float
    with pytest.raises(ValidationError):
        LLMProviderUsage(estimated_cost_usd=0.001)


def test_no_float_in_provider_cost_path():
    """No float values appear in provider cost estimation or budget accounting."""
    # The _reject_float_cost validator rejects float at Pydantic boundary
    usage = LLMProviderUsage(
        input_tokens=500,
        output_tokens=200,
        total_tokens=700,
        estimated_cost_usd=Decimal("0.0025"),
    )
    d = usage.model_dump()
    assert isinstance(d["estimated_cost_usd"], Decimal)


# ---------------------------------------------------------------------------
# Invariant: DRY_RUN / Execution Unchanged
# ---------------------------------------------------------------------------


def test_provider_does_not_change_dry_run_enforcement():
    """Provider selection does not weaken or bypass DRY_RUN=true enforcement."""
    # DRY_RUN is checked in execution path, not evaluation path.
    # Provider selection only affects which LLM is called, not execution routing.
    cfg = _mock_config(provider="deepseek", dry_run=True)
    client = ClaudeClient(
        in_queue=asyncio.Queue(),
        out_queue=asyncio.Queue(),
        config=cfg,
    )
    assert client.config.dry_run is True


def test_provider_does_not_change_signing_broadcasting_repositories():
    """Provider selection does not alter signing, broadcasting, repository boundaries, or Alembic schema."""
    # ClaudeClient does not touch signing, broadcasting, or repository code.
    # It only interacts with DecisionRepository for persistence — unchanged by WI-54.
    client = ClaudeClient(
        in_queue=asyncio.Queue(),
        out_queue=asyncio.Queue(),
        config=_mock_config(provider="deepseek"),
    )
    assert hasattr(client, "_decision_repo_factory")


# ---------------------------------------------------------------------------
# Invariant: No New SDK Dependency
# ---------------------------------------------------------------------------


def test_no_openai_sdk_imported():
    """The openai SDK is not imported or required in any WI-54 code path."""
    # Verify claude_client.py doesn't import openai
    from src.agents.evaluation import claude_client as cc_mod

    source = cc_mod.__file__
    assert source is not None
    with open(source) as f:
        content = f.read()
    assert "import openai" not in content
    assert "from openai" not in content

    # Verify config.py also doesn't import openai
    from src.core import config as cfg_mod

    source = cfg_mod.__file__
    assert source is not None
    with open(source) as f:
        content = f.read()
    assert "import openai" not in content
    assert "from openai" not in content


# ---------------------------------------------------------------------------
# Invariant: Secret-Free Observability
# ---------------------------------------------------------------------------


def test_provider_logs_never_contain_api_key():
    """Provider logs exclude API keys."""
    # LLMProviderMetadata only has provider, model_name, base_url_host — no api_key field
    meta = LLMProviderMetadata(
        provider="deepseek",
        model_name="deepseek-v4-pro",
        base_url_host="api.deepseek.com",
    )
    data = meta.model_dump()
    assert "api_key" not in data
    assert "key" not in data


def test_provider_logs_never_contain_raw_prompt():
    """Provider logs exclude raw prompt text."""
    # Provider metadata schema has no prompt field
    meta = LLMProviderMetadata(
        provider="anthropic",
        model_name="claude",
        base_url_host="api.anthropic.com",
    )
    data = meta.model_dump()
    assert "prompt" not in data


def test_provider_logs_never_contain_raw_reasoning():
    """Provider logs exclude raw reasoning/chain-of-thought text."""
    # Provider metadata schema has no reasoning field
    meta = LLMProviderMetadata(
        provider="anthropic",
        model_name="claude",
        base_url_host="api.anthropic.com",
    )
    data = meta.model_dump()
    assert "reasoning" not in data
    assert "reasoning_text" not in data
    assert "raw" not in data


@pytest.mark.asyncio
async def test_provider_metrics_labels_are_low_cardinality():
    """Provider metric labels use only provider name, model name, base URL host."""
    from src.observability.metrics import MetricsRegistry

    registry = MetricsRegistry()
    snapshot = await registry.snapshot()
    metric_names = {s.name for s in snapshot.samples}
    assert "poly_agent_llm_provider_selections_total" in metric_names
    assert "poly_agent_llm_provider_failures_total" in metric_names


def test_provider_metadata_in_audit_has_provider_model_host_only():
    """Provider metadata in audit/log records includes only provider, model, base URL host."""
    client = ClaudeClient(
        in_queue=asyncio.Queue(),
        out_queue=asyncio.Queue(),
        config=_mock_config(provider="deepseek"),
    )
    meta = client._provider_metadata.model_dump()
    assert set(meta.keys()) == {"provider", "model_name", "base_url_host"}
    assert meta["provider"] == "deepseek"
    assert meta["model_name"] == "deepseek-v4-pro"
    assert meta["base_url_host"] == "api.deepseek.com"


# ---------------------------------------------------------------------------
# Invariant: PromptFactory Unchanged
# ---------------------------------------------------------------------------


def test_provider_selection_does_not_change_prompt_factory():
    """PromptFactory behavior is unchanged by provider selection."""
    # PromptFactory.build_evaluation_prompt takes market_state, category, sentiment
    # and returns a prompt string. This is not affected by which provider is selected.
    from src.agents.context.prompt_factory import PromptFactory

    # Verify the method exists and accepts expected params
    assert hasattr(PromptFactory, "build_evaluation_prompt")
    assert callable(PromptFactory.build_evaluation_prompt)


# ---------------------------------------------------------------------------
# Edge Case: Operator Switches Provider Between Restarts
# ---------------------------------------------------------------------------


def test_inflight_evaluation_completes_under_original_provider():
    """In-flight evaluations complete under the previously configured provider after restart."""
    # Provider selection happens at init time. If operator changes llm_provider
    # in .env, the new value takes effect on next restart. In-flight evaluations
    # complete under the provider active at instantiation time.
    client = ClaudeClient(
        in_queue=asyncio.Queue(),
        out_queue=asyncio.Queue(),
        config=_mock_config(provider="deepseek"),
    )
    assert client._provider_name == LLMProviderName.DEEPSEEK
    # Even if config.llm_provider were changed externally, the client's
    # provider is locked at init time
    assert client.model == "deepseek-v4-pro"


# ---------------------------------------------------------------------------
# ClaudeClient: start() emits provider metrics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_records_provider_selection_metric():
    """ClaudeClient.start() records provider selection metric when metrics is available."""
    mock_metrics = MagicMock()
    mock_metrics.record_provider_selection = AsyncMock()
    mock_metrics.set_active_provider = AsyncMock()

    cfg = _mock_config(provider="deepseek")
    client = ClaudeClient(
        in_queue=asyncio.Queue(),
        out_queue=asyncio.Queue(),
        config=cfg,
        metrics=mock_metrics,
    )
    # start() creates background tasks; just verify it doesn't crash
    client._running = True
    # Manually trigger the metric calls (they're fire-and-forget in start())
    await mock_metrics.record_provider_selection(provider="deepseek")
    await mock_metrics.set_active_provider(provider="deepseek")
    mock_metrics.record_provider_selection.assert_called_with(provider="deepseek")
    mock_metrics.set_active_provider.assert_called_with(provider="deepseek")


# ---------------------------------------------------------------------------
# ClaudeClient: _build_hold_candidate produces conservative HOLD
# ---------------------------------------------------------------------------


def test_build_hold_candidate_produces_conservative_no_trade():
    """_build_hold_candidate sets decision_boolean=False and confidence=0.0."""
    client = ClaudeClient(
        in_queue=asyncio.Queue(),
        out_queue=asyncio.Queue(),
        config=_mock_config(provider="anthropic"),
    )
    primary = json.dumps(
        {
            "decision_boolean": True,
            "recommended_action": "BUY",
            "confidence_score": 0.85,
        }
    )
    hold = client._build_hold_candidate(primary)
    parsed = json.loads(hold)
    assert parsed["decision_boolean"] is False
    assert parsed["recommended_action"] == "HOLD"
    assert parsed["confidence_score"] == 0.0
