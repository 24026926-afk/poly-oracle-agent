"""
tests/unit/test_execution_router.py

RED-phase tests for WI-16 execution routing.

These tests intentionally codify the contract from
docs/prompts/P16-WI-16-execution-router.md before the implementation exists.
"""

from __future__ import annotations

import ast
import importlib
import inspect
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from src.core.config import AppConfig


ROUTER_MODULE_NAME = "src.agents.execution.execution_router"
ROUTER_MODULE_PATH = Path("src/agents/execution/execution_router.py")
SCHEMA_MODULE_NAME = "src.schemas.execution"
FORBIDDEN_IMPORT_PREFIXES = (
    "src.agents.context",
    "src.agents.ingestion",
    "src.agents.evaluation",
    "src.db",
)


def _load_router_module():
    try:
        return importlib.import_module(ROUTER_MODULE_NAME)
    except ModuleNotFoundError:
        pytest.fail(
            "Expected WI-16 module src.agents.execution.execution_router to exist.",
            pytrace=False,
        )
    except Exception as exc:
        pytest.fail(
            f"Execution router module import failed unexpectedly: {exc!r}",
            pytrace=False,
        )


def _load_execution_schema_module():
    try:
        return importlib.import_module(SCHEMA_MODULE_NAME)
    except ModuleNotFoundError:
        pytest.fail(
            "Expected WI-16 schema module src.schemas.execution to exist.",
            pytrace=False,
        )
    except Exception as exc:
        pytest.fail(
            f"Execution schema module import failed unexpectedly: {exc!r}",
            pytrace=False,
        )


def test_execution_router_contract_exists_and_has_one_public_method():
    module = _load_router_module()

    router_cls = getattr(module, "ExecutionRouter", None)
    assert router_cls is not None, "Expected ExecutionRouter class."
    assert inspect.isclass(router_cls)
    assert inspect.iscoroutinefunction(router_cls.route)

    public_methods = [
        name
        for name, member in inspect.getmembers(router_cls, predicate=inspect.isfunction)
        if not name.startswith("_")
    ]
    assert public_methods == ["route"]


def test_execution_router_module_has_no_forbidden_imports():
    if not ROUTER_MODULE_PATH.exists():
        pytest.fail(
            "Expected router implementation file at "
            "src/agents/execution/execution_router.py.",
            pytrace=False,
        )

    tree = ast.parse(ROUTER_MODULE_PATH.read_text())
    imported_modules: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported_modules.add(node.module)

    forbidden = sorted(
        module_name
        for module_name in imported_modules
        if module_name.startswith(FORBIDDEN_IMPORT_PREFIXES)
    )
    assert forbidden == []


def test_execution_result_schema_exists_and_rejects_float_financials():
    module = _load_execution_schema_module()

    action_cls = getattr(module, "ExecutionAction", None)
    result_cls = getattr(module, "ExecutionResult", None)

    assert action_cls is not None, "Expected ExecutionAction enum."
    assert result_cls is not None, "Expected ExecutionResult model."
    assert {member.value for member in action_cls} == {
        "SKIP",
        "DRY_RUN",
        "EXECUTED",
        "FAILED",
    }

    with pytest.raises(Exception):
        result_cls(
            action=action_cls.FAILED,
            reason="float_money_forbidden",
            order_payload=None,
            signed_order=None,
            kelly_fraction=0.2925,
            order_size_usdc=29.25,
            midpoint_probability=0.65,
            best_ask=0.66,
            bankroll_usdc=100.0,
            routed_at_utc=datetime.now(timezone.utc),
        )


def test_app_config_includes_execution_router_decimal_defaults():
    fields = AppConfig.model_fields

    assert "max_order_usdc" in fields, "Expected AppConfig.max_order_usdc field."
    assert "max_slippage_tolerance" in fields, (
        "Expected AppConfig.max_slippage_tolerance field."
    )

    max_order_field = fields["max_order_usdc"]
    slippage_field = fields["max_slippage_tolerance"]

    assert max_order_field.annotation is Decimal
    assert slippage_field.annotation is Decimal
    assert max_order_field.default == Decimal("50")
    assert slippage_field.default == Decimal("0.02")
