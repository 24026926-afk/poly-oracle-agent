"""
tests/unit/test_prompt_factory.py

Unit tests for PromptFactory.build_evaluation_prompt with MarketCategory variants.
"""

import pytest

from src.agents.context.prompt_factory import PromptFactory
from src.schemas.llm import MarketCategory


_SAMPLE_MARKET_STATE = {
    "condition_id": "0xabc123def456",
    "best_bid": 0.45,
    "best_ask": 0.55,
    "midpoint": 0.50,
    "spread": 0.10,
    "timestamp": 1700000000,
}


def test_build_prompt_default_is_general():
    prompt = PromptFactory.build_evaluation_prompt(_SAMPLE_MARKET_STATE)
    assert "Quantitative Developer" in prompt


def test_build_prompt_crypto_persona():
    prompt = PromptFactory.build_evaluation_prompt(
        _SAMPLE_MARKET_STATE, category=MarketCategory.CRYPTO,
    )
    assert "on-chain analyst" in prompt
    assert "Quantitative Developer" not in prompt


def test_build_prompt_politics_persona():
    prompt = PromptFactory.build_evaluation_prompt(
        _SAMPLE_MARKET_STATE, category=MarketCategory.POLITICS,
    )
    assert "political risk analyst" in prompt


def test_build_prompt_sports_persona():
    prompt = PromptFactory.build_evaluation_prompt(
        _SAMPLE_MARKET_STATE, category=MarketCategory.SPORTS,
    )
    assert "quantitative sports analyst" in prompt


@pytest.mark.parametrize("category", list(MarketCategory))
def test_all_variants_contain_json_schema_block(category):
    prompt = PromptFactory.build_evaluation_prompt(
        _SAMPLE_MARKET_STATE, category=category,
    )
    assert "### CRITICAL OUTPUT FORMAT" in prompt
    assert "JSON Schema" in prompt


def test_backward_compatibility():
    prompt = PromptFactory.build_evaluation_prompt(_SAMPLE_MARKET_STATE)
    assert isinstance(prompt, str)
    assert len(prompt) > 0
