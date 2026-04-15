"""
tests/integration/test_wi33_backtest_runner.py

RED-phase integration tests for WI-33 Backtesting Framework.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from decimal import Decimal
import importlib
import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


RUNNER_MODULE = "src.backtest_runner"
SCHEMA_MODULE = "src.schemas.execution"
RUNNER_PATH = Path("src/backtest_runner.py")
FORBIDDEN_IMPORTS = {
    "src.agents.ingestion.ws_client",
    "src.agents.ingestion.rest_client",
}


def _load_module(name: str):
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError:
        pytest.fail(f"Expected module {name} to exist.", pytrace=False)
    except Exception as exc:  # pragma: no cover - RED diagnostics
        pytest.fail(f"Failed to import {name}: {exc!r}", pytrace=False)


def _make_config(schema_module, data_dir: Path, **overrides):
    defaults = {
        "data_dir": str(data_dir),
        "start_date": None,
        "end_date": None,
        "initial_bankroll_usdc": Decimal("1000"),
        "kelly_fraction": Decimal("0.25"),
        "min_confidence": Decimal("0.75"),
        "min_ev_threshold": Decimal("0.02"),
        "dry_run": True,
    }
    defaults.update(overrides)
    return schema_module.BacktestConfig(**defaults)


def _build_runner(runner_module, config):
    runner_cls = getattr(runner_module, "BacktestRunner", None)
    assert runner_cls is not None, "Expected BacktestRunner in src.backtest_runner."
    try:
        return runner_cls(config=config)
    except TypeError:
        return runner_cls(config)


async def _run_runner(runner):
    run_method = getattr(runner, "run", None)
    assert run_method is not None, "Expected BacktestRunner.run()."
    result = run_method()
    if inspect.isawaitable(result):
        return await result
    return result


def _snapshot_field(snapshot: Any, field_name: str) -> Any:
    if isinstance(snapshot, dict):
        return snapshot[field_name]
    return getattr(snapshot, field_name)


def _decision_field(decision: Any, field_name: str) -> Any:
    if isinstance(decision, dict):
        return decision[field_name]
    return getattr(decision, field_name)


def _as_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    raise TypeError(f"Unsupported timestamp type: {type(value)!r}")


def _as_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


@pytest.mark.asyncio
async def test_backtest_runner_refuses_dry_run_false_at_initialization(tmp_path):
    runner = _load_module(RUNNER_MODULE)
    schemas = _load_module(SCHEMA_MODULE)
    cfg = _make_config(schemas, tmp_path, dry_run=False)

    with pytest.raises(RuntimeError):
        _build_runner(runner, cfg)


@pytest.mark.asyncio
async def test_backtest_runner_replay_pipeline_and_metrics_contract(
    tmp_path, monkeypatch
):
    runner_module = _load_module(RUNNER_MODULE)
    schemas = _load_module(SCHEMA_MODULE)
    cfg = _make_config(schemas, tmp_path)

    snapshots = [
        {
            "token_id": "tok-a",
            "timestamp_utc": "2026-01-01T02:00:00Z",
            "best_bid": Decimal("0.40"),
            "best_ask": Decimal("0.42"),
            "midpoint": Decimal("0.41"),
            "realized_pnl_usdc": Decimal("5"),
        },
        {
            "token_id": "tok-a",
            "timestamp_utc": "2026-01-01T01:00:00Z",
            "best_bid": Decimal("0.45"),
            "best_ask": Decimal("0.47"),
            "midpoint": Decimal("0.46"),
            "realized_pnl_usdc": Decimal("-2"),
        },
        {
            "token_id": "tok-b",
            "timestamp_utc": "2026-01-01T03:00:00Z",
            "best_bid": Decimal("0.55"),
            "best_ask": Decimal("0.57"),
            "midpoint": Decimal("0.56"),
            "realized_pnl_usdc": Decimal("0"),
        },
    ]

    calls = {
        "aggregator": 0,
        "prompt_factory": 0,
        "claude": 0,
        "gatekeeper": 0,
        "execution_router": 0,
    }
    replay_order: list[datetime] = []
    router_call_kwargs: list[dict[str, Any]] = []

    class FakeBacktestDataLoader:
        def __init__(self, *args, **kwargs):
            self._snapshots = list(snapshots)

        def load_all(self):
            return list(self._snapshots)

    class FakeDataAggregator:
        def __init__(self, *args, **kwargs):
            pass

        def build_context(self, snapshot):
            calls["aggregator"] += 1
            replay_order.append(
                _as_datetime(_snapshot_field(snapshot, "timestamp_utc"))
            )
            return {"snapshot": snapshot}

    class FakePromptFactory:
        @staticmethod
        def build_evaluation_prompt(context):
            calls["prompt_factory"] += 1
            return {
                "prompt": "evaluate_snapshot",
                "snapshot": context["snapshot"],
            }

    class FakeClaudeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def evaluate(self, prompt):
            calls["claude"] += 1
            return {
                "raw_decision_payload": True,
                "snapshot": prompt["snapshot"],
            }

    gatekeeper_outcomes = [
        SimpleNamespace(
            decision_boolean=True,
            recommended_action=SimpleNamespace(value="BUY"),
            position_size_pct=Decimal("0.02"),
            expected_value=Decimal("0.05"),
            confidence_score=Decimal("0.90"),
            reasoning_log="pass_trade_1",
            market_context=SimpleNamespace(condition_id="tok-a"),
        ),
        SimpleNamespace(
            decision_boolean=True,
            recommended_action=SimpleNamespace(value="BUY"),
            position_size_pct=Decimal("0.02"),
            expected_value=Decimal("0.04"),
            confidence_score=Decimal("0.88"),
            reasoning_log="pass_trade_2",
            market_context=SimpleNamespace(condition_id="tok-a"),
        ),
        SimpleNamespace(
            decision_boolean=False,
            recommended_action=SimpleNamespace(value="HOLD"),
            position_size_pct=Decimal("0"),
            expected_value=Decimal("-0.01"),
            confidence_score=Decimal("0.60"),
            reasoning_log="failed_gatekeeper",
            market_context=SimpleNamespace(condition_id="tok-b"),
        ),
    ]

    def _fake_gatekeeper_validate(_raw_decision):
        calls["gatekeeper"] += 1
        return gatekeeper_outcomes[calls["gatekeeper"] - 1]

    class FakeExecutionRouter:
        def __init__(self, *args, **kwargs):
            pass

        async def route(self, *args, **kwargs):
            calls["execution_router"] += 1
            router_call_kwargs.append(dict(kwargs))
            pnl = Decimal("5") if calls["execution_router"] == 1 else Decimal("-2")
            return SimpleNamespace(
                action="DRY_RUN",
                reason="backtest_dry_run",
                realized_pnl_usdc=pnl,
            )

    monkeypatch.setattr(runner_module, "BacktestDataLoader", FakeBacktestDataLoader)
    monkeypatch.setattr(runner_module, "DataAggregator", FakeDataAggregator)
    monkeypatch.setattr(runner_module, "PromptFactory", FakePromptFactory)
    monkeypatch.setattr(runner_module, "ClaudeClient", FakeClaudeClient)
    monkeypatch.setattr(runner_module, "ExecutionRouter", FakeExecutionRouter)
    monkeypatch.setattr(
        runner_module,
        "LLMEvaluationResponse",
        SimpleNamespace(model_validate_json=_fake_gatekeeper_validate),
    )

    backtest_runner = _build_runner(runner_module, cfg)
    report = await _run_runner(backtest_runner)

    assert replay_order == sorted(replay_order)
    assert calls["aggregator"] == len(snapshots)
    assert calls["prompt_factory"] == len(snapshots)
    assert calls["claude"] == len(snapshots)
    assert calls["gatekeeper"] == len(snapshots)
    assert calls["execution_router"] == 2

    for kwargs in router_call_kwargs:
        assert kwargs.get("dry_run") is True

    assert hasattr(report, "total_trades")
    assert hasattr(report, "win_rate")
    assert hasattr(report, "net_pnl_usdc")
    assert hasattr(report, "max_drawdown_usdc")
    assert hasattr(report, "sharpe_ratio")
    assert hasattr(report, "per_market_stats")
    assert hasattr(report, "decisions")
    assert hasattr(report, "started_at_utc")
    assert hasattr(report, "completed_at_utc")
    assert hasattr(report, "config_snapshot")

    assert report.total_trades == 2
    assert _as_decimal(report.win_rate) == Decimal("0.5")
    assert _as_decimal(report.net_pnl_usdc) == Decimal("3")
    assert _as_decimal(report.max_drawdown_usdc) == Decimal("2")

    decisions = list(report.decisions)
    assert len(decisions) == len(snapshots)

    failed_decisions = [
        d for d in decisions if str(_decision_field(d, "gatekeeper_result")) == "FAILED"
    ]
    assert len(failed_decisions) == 1
    assert str(_decision_field(failed_decisions[0], "action")) == "HOLD"


@pytest.mark.asyncio
async def test_backtest_runner_zero_database_writes(tmp_path, monkeypatch):
    runner_module = _load_module(RUNNER_MODULE)
    schemas = _load_module(SCHEMA_MODULE)
    cfg = _make_config(schemas, tmp_path)

    snapshots = [
        {
            "token_id": "tok-a",
            "timestamp_utc": "2026-01-01T01:00:00Z",
            "best_bid": Decimal("0.45"),
            "best_ask": Decimal("0.47"),
            "midpoint": Decimal("0.46"),
        }
    ]

    class FakeBacktestDataLoader:
        def __init__(self, *args, **kwargs):
            pass

        def load_all(self):
            return snapshots

    class FakeDataAggregator:
        def __init__(self, *args, **kwargs):
            pass

        def build_context(self, snapshot):
            return {"snapshot": snapshot}

    class FakePromptFactory:
        @staticmethod
        def build_evaluation_prompt(context):
            return {"prompt": "evaluate", "snapshot": context["snapshot"]}

    class FakeClaudeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def evaluate(self, _prompt):
            return {"candidate": "ok"}

    class FakeExecutionRouter:
        def __init__(self, *args, **kwargs):
            pass

        async def route(self, *args, **kwargs):
            return SimpleNamespace(
                action="DRY_RUN",
                reason="backtest_dry_run",
                realized_pnl_usdc=Decimal("1"),
            )

    monkeypatch.setattr(runner_module, "BacktestDataLoader", FakeBacktestDataLoader)
    monkeypatch.setattr(runner_module, "DataAggregator", FakeDataAggregator)
    monkeypatch.setattr(runner_module, "PromptFactory", FakePromptFactory)
    monkeypatch.setattr(runner_module, "ClaudeClient", FakeClaudeClient)
    monkeypatch.setattr(runner_module, "ExecutionRouter", FakeExecutionRouter)
    monkeypatch.setattr(
        runner_module,
        "LLMEvaluationResponse",
        SimpleNamespace(
            model_validate_json=lambda _raw: SimpleNamespace(
                decision_boolean=True,
                recommended_action=SimpleNamespace(value="BUY"),
                position_size_pct=Decimal("0.02"),
                expected_value=Decimal("0.05"),
                confidence_score=Decimal("0.90"),
                reasoning_log="pass_trade",
                market_context=SimpleNamespace(condition_id="tok-a"),
            )
        ),
    )

    import sqlalchemy.ext.asyncio as sa_async

    def _forbidden_sync(*args, **kwargs):
        raise AssertionError("Database write in backtest path is forbidden.")

    async def _forbidden_async(*args, **kwargs):
        raise AssertionError("Database write in backtest path is forbidden.")

    monkeypatch.setattr(sa_async.AsyncSession, "add", _forbidden_sync, raising=False)
    monkeypatch.setattr(
        sa_async.AsyncSession, "commit", _forbidden_async, raising=False
    )
    monkeypatch.setattr(sa_async.AsyncSession, "flush", _forbidden_async, raising=False)
    monkeypatch.setattr(
        sa_async.AsyncSession, "execute", _forbidden_async, raising=False
    )

    backtest_runner = _build_runner(runner_module, cfg)
    await _run_runner(backtest_runner)


@pytest.mark.asyncio
async def test_backtest_cli_entrypoint_writes_json_output(tmp_path, monkeypatch):
    runner_module = _load_module(RUNNER_MODULE)
    schemas = _load_module(SCHEMA_MODULE)

    data_dir = tmp_path / "historical"
    data_dir.mkdir(parents=True, exist_ok=True)
    output_path = tmp_path / "backtest_report.json"

    cfg = _make_config(schemas, data_dir)
    decision = schemas.BacktestDecision(
        token_id="tok-a",
        timestamp_utc=datetime(2026, 1, 1, tzinfo=UTC),
        decision=True,
        action="BUY",
        position_size_usdc=Decimal("10"),
        ev=Decimal("0.05"),
        confidence=Decimal("0.90"),
        gatekeeper_result="PASSED",
        reason="positive_edge",
    )
    market_stats = schemas.BacktestMarketStats(
        token_id="tok-a",
        total_decisions=1,
        trades_executed=1,
        win_rate=Decimal("1"),
        net_pnl_usdc=Decimal("1"),
    )
    report = schemas.BacktestReport(
        total_trades=1,
        win_rate=Decimal("1"),
        net_pnl_usdc=Decimal("1"),
        max_drawdown_usdc=Decimal("0"),
        sharpe_ratio=Decimal("0"),
        per_market_stats={"tok-a": market_stats},
        decisions=[decision],
        started_at_utc=datetime(2026, 1, 1, tzinfo=UTC),
        completed_at_utc=datetime(2026, 1, 1, 1, tzinfo=UTC),
        config_snapshot=cfg,
    )

    class FakeBacktestRunner:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self):
            return report

    monkeypatch.setattr(runner_module, "BacktestRunner", FakeBacktestRunner)

    main = getattr(runner_module, "main", None)
    assert main is not None, "Expected CLI entrypoint function main()."

    result = main(
        [
            "--data-dir",
            str(data_dir),
            "--output",
            str(output_path),
        ]
    )
    if inspect.isawaitable(result):
        await result

    assert output_path.exists()
    raw = json.loads(output_path.read_text(encoding="utf-8"))
    assert "total_trades" in raw
    assert "net_pnl_usdc" in raw
    assert "decisions" in raw


def test_backtest_runner_module_has_no_live_ingestion_imports():
    if not RUNNER_PATH.exists():
        pytest.fail("Expected src/backtest_runner.py to exist.", pytrace=False)

    tree = ast.parse(RUNNER_PATH.read_text(encoding="utf-8"))
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
        if module_name in FORBIDDEN_IMPORTS
    )
    assert forbidden == []
