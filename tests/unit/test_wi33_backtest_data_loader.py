"""
tests/unit/test_wi33_backtest_data_loader.py

RED-phase unit tests for WI-33 Backtesting Framework.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
import importlib
import inspect
import json
from pathlib import Path
from typing import Any

import pytest


RUNNER_MODULE = "src.backtest_runner"
SCHEMA_MODULE = "src.schemas.execution"


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
        "dry_run": True,
    }
    defaults.update(overrides)
    return schema_module.BacktestConfig(**defaults)


def _build_loader(runner_module, config):
    loader_cls = getattr(runner_module, "BacktestDataLoader", None)
    assert loader_cls is not None, "Expected BacktestDataLoader in src.backtest_runner."
    try:
        return loader_cls(config=config)
    except TypeError:
        return loader_cls(config)


async def _load_all(loader):
    load_all = getattr(loader, "load_all", None)
    assert load_all is not None, "Expected BacktestDataLoader.load_all()."
    result = load_all()
    if inspect.isawaitable(result):
        return await result
    return result


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _snapshot_field(snapshot: Any, field_name: str) -> Any:
    if isinstance(snapshot, dict):
        return snapshot[field_name]
    return getattr(snapshot, field_name)


def _as_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    if isinstance(value, str):
        normalised = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalised)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    raise TypeError(f"Unsupported timestamp type: {type(value)!r}")


def _as_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def test_backtest_schemas_exist_and_are_frozen():
    schemas = _load_module(SCHEMA_MODULE)

    required = {
        "BacktestConfig",
        "BacktestReport",
        "BacktestMarketStats",
        "BacktestDecision",
    }
    missing = [name for name in required if not hasattr(schemas, name)]
    assert missing == [], f"Missing WI-33 schemas: {missing}"

    cfg = schemas.BacktestConfig(
        data_dir="/tmp/historical",
        start_date=None,
        end_date=None,
        initial_bankroll_usdc=Decimal("1000"),
        dry_run=True,
    )
    decision = schemas.BacktestDecision(
        token_id="token-yes-001",
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
        token_id="token-yes-001",
        total_decisions=1,
        trades_executed=1,
        win_rate=Decimal("1"),
        net_pnl_usdc=Decimal("2.5"),
    )
    report = schemas.BacktestReport(
        total_trades=1,
        win_rate=Decimal("1"),
        net_pnl_usdc=Decimal("2.5"),
        max_drawdown_usdc=Decimal("0"),
        sharpe_ratio=Decimal("0"),
        per_market_stats={"token-yes-001": market_stats},
        decisions=[decision],
        started_at_utc=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        completed_at_utc=datetime(2026, 1, 1, 1, 0, tzinfo=UTC),
        config_snapshot=cfg,
    )

    with pytest.raises(Exception):
        report.total_trades = 2

    with pytest.raises(Exception):
        cfg.dry_run = False


def test_backtest_config_defaults_match_wi33_contract():
    schemas = _load_module(SCHEMA_MODULE)
    cfg = schemas.BacktestConfig(
        data_dir="/tmp/historical",
        start_date=None,
        end_date=None,
        initial_bankroll_usdc=Decimal("1000"),
        dry_run=True,
    )

    assert cfg.kelly_fraction == Decimal("0.25")
    assert cfg.min_confidence == Decimal("0.75")
    assert cfg.min_ev_threshold == Decimal("0.02")
    assert cfg.dry_run is True


@pytest.mark.parametrize(
    "field_name",
    ["initial_bankroll_usdc", "kelly_fraction", "min_confidence", "min_ev_threshold"],
)
def test_backtest_config_rejects_float_financial_fields(field_name):
    schemas = _load_module(SCHEMA_MODULE)
    payload = {
        "data_dir": "/tmp/historical",
        "start_date": None,
        "end_date": None,
        "initial_bankroll_usdc": Decimal("1000"),
        "kelly_fraction": Decimal("0.25"),
        "min_confidence": Decimal("0.75"),
        "min_ev_threshold": Decimal("0.02"),
        "dry_run": True,
    }
    payload[field_name] = 0.5

    with pytest.raises(Exception):
        schemas.BacktestConfig(**payload)


@pytest.mark.parametrize(
    "field_name",
    ["position_size_usdc", "ev", "confidence"],
)
def test_backtest_decision_rejects_float_financial_fields(field_name):
    schemas = _load_module(SCHEMA_MODULE)
    payload = {
        "token_id": "token-yes-001",
        "timestamp_utc": datetime(2026, 1, 1, tzinfo=UTC),
        "decision": True,
        "action": "BUY",
        "position_size_usdc": Decimal("10"),
        "ev": Decimal("0.05"),
        "confidence": Decimal("0.90"),
        "gatekeeper_result": "PASSED",
        "reason": "positive_edge",
    }
    payload[field_name] = 0.5

    with pytest.raises(Exception):
        schemas.BacktestDecision(**payload)


@pytest.mark.parametrize(
    "field_name",
    ["win_rate", "net_pnl_usdc"],
)
def test_backtest_market_stats_rejects_float_financial_fields(field_name):
    schemas = _load_module(SCHEMA_MODULE)
    payload = {
        "token_id": "token-yes-001",
        "total_decisions": 3,
        "trades_executed": 2,
        "win_rate": Decimal("0.5"),
        "net_pnl_usdc": Decimal("4.2"),
    }
    payload[field_name] = 0.5

    with pytest.raises(Exception):
        schemas.BacktestMarketStats(**payload)


@pytest.mark.parametrize(
    "field_name",
    ["win_rate", "net_pnl_usdc", "max_drawdown_usdc", "sharpe_ratio"],
)
def test_backtest_report_rejects_float_financial_fields(field_name):
    schemas = _load_module(SCHEMA_MODULE)
    cfg = schemas.BacktestConfig(
        data_dir="/tmp/historical",
        start_date=None,
        end_date=None,
        initial_bankroll_usdc=Decimal("1000"),
        dry_run=True,
    )
    decision = schemas.BacktestDecision(
        token_id="token-yes-001",
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
        token_id="token-yes-001",
        total_decisions=1,
        trades_executed=1,
        win_rate=Decimal("1"),
        net_pnl_usdc=Decimal("1"),
    )
    payload = {
        "total_trades": 1,
        "win_rate": Decimal("1"),
        "net_pnl_usdc": Decimal("1"),
        "max_drawdown_usdc": Decimal("0"),
        "sharpe_ratio": Decimal("0"),
        "per_market_stats": {"token-yes-001": market_stats},
        "decisions": [decision],
        "started_at_utc": datetime(2026, 1, 1, tzinfo=UTC),
        "completed_at_utc": datetime(2026, 1, 1, 1, tzinfo=UTC),
        "config_snapshot": cfg,
    }
    payload[field_name] = 0.5

    with pytest.raises(Exception):
        schemas.BacktestReport(**payload)


@pytest.mark.asyncio
async def test_data_loader_parses_and_sorts_chronologically_across_markets(tmp_path):
    runner = _load_module(RUNNER_MODULE)
    schemas = _load_module(SCHEMA_MODULE)
    cfg = _make_config(schemas, tmp_path)

    _write_json(
        tmp_path / "tok-a_2026-01-01.json",
        [
            {
                "timestamp_utc": "2026-01-01T12:00:00Z",
                "best_bid": "0.44",
                "best_ask": "0.46",
                "midpoint": "0.45",
            },
            {
                "timestamp_utc": "2026-01-01T09:00:00Z",
                "best_bid": "0.39",
                "best_ask": "0.41",
                "midpoint": "0.40",
            },
        ],
    )
    _write_json(
        tmp_path / "tok-b_2026-01-01.json",
        [
            {
                "timestamp_utc": "2026-01-01T10:00:00Z",
                "best_bid": "0.54",
                "best_ask": "0.56",
                "midpoint": "0.55",
            }
        ],
    )

    loader = _build_loader(runner, cfg)
    snapshots = await _load_all(loader)

    assert len(snapshots) == 3
    timestamps = [_as_datetime(_snapshot_field(s, "timestamp_utc")) for s in snapshots]
    assert timestamps == sorted(timestamps)

    token_ids = [_snapshot_field(s, "token_id") for s in snapshots]
    assert token_ids.count("tok-a") == 2
    assert token_ids.count("tok-b") == 1


@pytest.mark.asyncio
async def test_data_loader_applies_start_and_end_date_filters(tmp_path):
    runner = _load_module(RUNNER_MODULE)
    schemas = _load_module(SCHEMA_MODULE)
    cfg = _make_config(
        schemas,
        tmp_path,
        start_date=date(2026, 1, 2),
        end_date=date(2026, 1, 2),
    )
    _write_json(
        tmp_path / "tok-a_2026-01-01.json",
        [
            {
                "timestamp_utc": "2026-01-01T12:00:00Z",
                "best_bid": "0.44",
                "best_ask": "0.46",
                "midpoint": "0.45",
            }
        ],
    )
    _write_json(
        tmp_path / "tok-a_2026-01-02.json",
        [
            {
                "timestamp_utc": "2026-01-02T12:00:00Z",
                "best_bid": "0.49",
                "best_ask": "0.51",
                "midpoint": "0.50",
            }
        ],
    )
    _write_json(
        tmp_path / "tok-a_2026-01-03.json",
        [
            {
                "timestamp_utc": "2026-01-03T12:00:00Z",
                "best_bid": "0.54",
                "best_ask": "0.56",
                "midpoint": "0.55",
            }
        ],
    )

    loader = _build_loader(runner, cfg)
    snapshots = await _load_all(loader)
    assert len(snapshots) == 1

    ts = _as_datetime(_snapshot_field(snapshots[0], "timestamp_utc"))
    assert ts.date() == date(2026, 1, 2)


@pytest.mark.asyncio
async def test_data_loader_empty_directory_returns_empty_list(tmp_path):
    runner = _load_module(RUNNER_MODULE)
    schemas = _load_module(SCHEMA_MODULE)
    cfg = _make_config(schemas, tmp_path)
    loader = _build_loader(runner, cfg)

    snapshots = await _load_all(loader)
    assert snapshots == []


@pytest.mark.asyncio
async def test_data_loader_empty_valid_file_returns_empty_list(tmp_path):
    runner = _load_module(RUNNER_MODULE)
    schemas = _load_module(SCHEMA_MODULE)
    cfg = _make_config(schemas, tmp_path)

    _write_json(tmp_path / "tok-a_2026-01-01.json", [])
    loader = _build_loader(runner, cfg)

    snapshots = await _load_all(loader)
    assert snapshots == []


@pytest.mark.asyncio
async def test_data_loader_raises_backtest_data_error_for_malformed_json(tmp_path):
    runner = _load_module(RUNNER_MODULE)
    schemas = _load_module(SCHEMA_MODULE)
    cfg = _make_config(schemas, tmp_path)

    bad_file = tmp_path / "tok-a_2026-01-01.json"
    bad_file.write_text("{not valid json", encoding="utf-8")

    error_cls = getattr(runner, "BacktestDataError", None)
    assert error_cls is not None, "Expected BacktestDataError in src.backtest_runner."

    loader = _build_loader(runner, cfg)
    with pytest.raises(error_cls):
        await _load_all(loader)


@pytest.mark.asyncio
async def test_data_loader_raises_backtest_data_error_for_missing_required_fields(tmp_path):
    runner = _load_module(RUNNER_MODULE)
    schemas = _load_module(SCHEMA_MODULE)
    cfg = _make_config(schemas, tmp_path)

    _write_json(
        tmp_path / "tok-a_2026-01-01.json",
        [
            {
                "timestamp_utc": "2026-01-01T12:00:00Z",
                "best_bid": "0.44",
                "best_ask": "0.46",
            }
        ],
    )

    error_cls = getattr(runner, "BacktestDataError", None)
    assert error_cls is not None, "Expected BacktestDataError in src.backtest_runner."

    loader = _build_loader(runner, cfg)
    with pytest.raises(error_cls):
        await _load_all(loader)


@pytest.mark.asyncio
async def test_data_loader_raises_backtest_data_error_for_invalid_numeric_values(tmp_path):
    runner = _load_module(RUNNER_MODULE)
    schemas = _load_module(SCHEMA_MODULE)
    cfg = _make_config(schemas, tmp_path)

    _write_json(
        tmp_path / "tok-a_2026-01-01.json",
        [
            {
                "timestamp_utc": "2026-01-01T12:00:00Z",
                "best_bid": "not-a-number",
                "best_ask": "0.46",
                "midpoint": "0.45",
            }
        ],
    )

    error_cls = getattr(runner, "BacktestDataError", None)
    assert error_cls is not None, "Expected BacktestDataError in src.backtest_runner."

    loader = _build_loader(runner, cfg)
    with pytest.raises(error_cls):
        await _load_all(loader)


@pytest.mark.asyncio
async def test_data_loader_raises_backtest_data_error_for_invalid_timestamps(tmp_path):
    runner = _load_module(RUNNER_MODULE)
    schemas = _load_module(SCHEMA_MODULE)
    cfg = _make_config(schemas, tmp_path)

    _write_json(
        tmp_path / "tok-a_2026-01-01.json",
        [
            {
                "timestamp_utc": "not-a-real-timestamp",
                "best_bid": "0.44",
                "best_ask": "0.46",
                "midpoint": "0.45",
            }
        ],
    )

    error_cls = getattr(runner, "BacktestDataError", None)
    assert error_cls is not None, "Expected BacktestDataError in src.backtest_runner."

    loader = _build_loader(runner, cfg)
    with pytest.raises(error_cls):
        await _load_all(loader)


@pytest.mark.asyncio
async def test_data_loader_emits_decimal_safe_monetary_fields(tmp_path):
    runner = _load_module(RUNNER_MODULE)
    schemas = _load_module(SCHEMA_MODULE)
    cfg = _make_config(schemas, tmp_path)

    _write_json(
        tmp_path / "tok-a_2026-01-01.json",
        [
            {
                "timestamp_utc": "2026-01-01T12:00:00Z",
                "best_bid": "0.44",
                "best_ask": "0.46",
                "midpoint": "0.45",
            }
        ],
    )

    loader = _build_loader(runner, cfg)
    snapshots = await _load_all(loader)
    assert len(snapshots) == 1

    for field_name in ("best_bid", "best_ask", "midpoint"):
        value = _snapshot_field(snapshots[0], field_name)
        decimal_value = _as_decimal(value)
        assert isinstance(decimal_value, Decimal)
