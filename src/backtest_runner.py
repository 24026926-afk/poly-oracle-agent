"""
src/backtest_runner.py

WI-33 offline backtesting framework.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Sequence

import structlog

from src.agents.context.aggregator import DataAggregator
from src.agents.context.prompt_factory import PromptFactory
from src.agents.evaluation.claude_client import ClaudeClient
from src.agents.execution.execution_router import ExecutionRouter
from src.schemas.execution import (
    BacktestConfig,
    BacktestDecision,
    BacktestMarketStats,
    BacktestReport,
)
from src.schemas.llm import LLMEvaluationResponse

logger = structlog.get_logger(__name__)

_ZERO = Decimal("0")
_FILENAME_PATTERN = re.compile(
    r"^(?P<token_id>.+)_(?P<file_date>\d{4}-\d{2}-\d{2})\.json$"
)


class BacktestDataError(ValueError):
    """Raised when historical backtest JSON input is malformed."""


@dataclass(frozen=True)
class BacktestSnapshot:
    """Typed replay record loaded from historical JSON files."""

    token_id: str
    timestamp_utc: datetime
    best_bid: Decimal
    best_ask: Decimal
    midpoint: Decimal
    realized_pnl_usdc: Decimal = _ZERO


class BacktestDataLoader:
    """Loader for WI-33 historical CLOB snapshots."""

    def __init__(self, config: BacktestConfig) -> None:
        self._config = config

    def load_all(self) -> list[BacktestSnapshot]:
        data_dir = Path(self._config.data_dir)
        if not data_dir.exists() or not data_dir.is_dir():
            return []

        snapshots: list[BacktestSnapshot] = []
        for json_file in sorted(data_dir.glob("*.json")):
            match = _FILENAME_PATTERN.match(json_file.name)
            if match is None:
                continue

            token_id = match.group("token_id")
            try:
                records = json.loads(json_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise BacktestDataError(
                    f"Malformed JSON in {json_file}: {exc}"
                ) from exc
            except OSError as exc:
                raise BacktestDataError(f"Failed reading {json_file}: {exc}") from exc

            if not isinstance(records, list):
                raise BacktestDataError(
                    f"Expected top-level list in {json_file}, got {type(records)!r}"
                )

            snapshots.extend(
                self._parse_file_records(
                    token_id=token_id,
                    records=records,
                    source_file=json_file,
                )
            )

        snapshots.sort(key=lambda snapshot: snapshot.timestamp_utc)
        return snapshots

    def _parse_file_records(
        self,
        *,
        token_id: str,
        records: list[Any],
        source_file: Path,
    ) -> list[BacktestSnapshot]:
        parsed: list[BacktestSnapshot] = []

        for index, record in enumerate(records):
            if not isinstance(record, dict):
                raise BacktestDataError(
                    f"Record {index} in {source_file} must be an object."
                )

            required_fields = {"timestamp_utc", "best_bid", "best_ask", "midpoint"}
            missing_fields = sorted(required_fields - set(record))
            if missing_fields:
                raise BacktestDataError(
                    f"Record {index} in {source_file} missing fields: {missing_fields}"
                )

            timestamp_utc = self._parse_timestamp(
                record["timestamp_utc"],
                source_file=source_file,
                index=index,
            )

            if not self._is_within_date_window(timestamp_utc.date()):
                continue

            try:
                best_bid = self._parse_decimal(record["best_bid"])
                best_ask = self._parse_decimal(record["best_ask"])
                midpoint = self._parse_decimal(record["midpoint"])
                realized_pnl_usdc = self._parse_decimal(
                    record.get("realized_pnl_usdc", _ZERO)
                )
            except (ArithmeticError, ValueError, TypeError) as exc:
                raise BacktestDataError(
                    f"Record {index} in {source_file} has invalid numeric values: {exc}"
                ) from exc

            parsed.append(
                BacktestSnapshot(
                    token_id=token_id,
                    timestamp_utc=timestamp_utc,
                    best_bid=best_bid,
                    best_ask=best_ask,
                    midpoint=midpoint,
                    realized_pnl_usdc=realized_pnl_usdc,
                )
            )

        return parsed

    def _is_within_date_window(self, snapshot_date: date) -> bool:
        if (
            self._config.start_date is not None
            and snapshot_date < self._config.start_date
        ):
            return False
        if self._config.end_date is not None and snapshot_date > self._config.end_date:
            return False
        return True

    @staticmethod
    def _parse_timestamp(value: Any, *, source_file: Path, index: int) -> datetime:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str):
            normalised = value.replace("Z", "+00:00")
            try:
                parsed = datetime.fromisoformat(normalised)
            except ValueError as exc:
                raise BacktestDataError(
                    f"Invalid timestamp in {source_file} record {index}: {value!r}"
                ) from exc
        else:
            raise BacktestDataError(
                f"Unsupported timestamp type in {source_file} record {index}: {type(value)!r}"
            )

        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    @staticmethod
    def _parse_decimal(value: Any) -> Decimal:
        if isinstance(value, Decimal):
            return value
        if isinstance(value, float):
            raise ValueError("Float values are forbidden in financial backtest paths")
        return Decimal(str(value))


class _FallbackDataAggregator:
    """Offline context builder used when the runtime DataAggregator is unavailable."""

    def build_context(self, snapshot: Any) -> dict[str, Any]:
        return {
            "condition_id": _snapshot_field(snapshot, "token_id"),
            "best_bid": float(_decimal_field(snapshot, "best_bid")),
            "best_ask": float(_decimal_field(snapshot, "best_ask")),
            "midpoint": float(_decimal_field(snapshot, "midpoint")),
            "spread": float(
                _decimal_field(snapshot, "best_ask")
                - _decimal_field(snapshot, "best_bid")
            ),
            "timestamp": _timestamp_field(snapshot).isoformat(),
        }


class _FallbackClaudeClient:
    """Offline fallback evaluator that always returns conservative HOLD."""

    async def evaluate(self, _prompt: Any) -> dict[str, Any]:
        return {
            "market_context": {
                "condition_id": "0000000000",
                "outcome_evaluated": "YES",
                "best_bid": 0.5,
                "best_ask": 0.5,
                "midpoint": 0.5,
            },
            "probabilistic_estimate": {"p_true": 0.5, "p_market": 0.5},
            "risk_assessment": {
                "liquidity_risk_score": 0.5,
                "resolution_risk_score": 0.5,
                "information_asymmetry_flag": False,
                "risk_notes": (
                    "Fallback offline evaluator used for WI-33 when no Claude "
                    "evaluate() adapter is available in this runtime."
                ),
            },
            "confidence_score": 0.0,
            "decision_boolean": False,
            "recommended_action": "HOLD",
            "reasoning_log": (
                "Fallback evaluator defaulted to HOLD because no runnable "
                "Claude evaluate() adapter was supplied for this environment."
            ),
        }


class _FallbackExecutionRouter:
    """Offline fallback router that simulates dry-run routing only."""

    async def route(self, *_args: Any, **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            action="DRY_RUN",
            reason="backtest_dry_run_fallback",
            realized_pnl_usdc=Decimal("0"),
        )


class BacktestRunner:
    """Offline WI-33 replay coordinator."""

    def __init__(
        self,
        config: BacktestConfig,
        *,
        data_loader: BacktestDataLoader | None = None,
        data_aggregator: Any | None = None,
        prompt_factory: Any | None = None,
        claude_client: Any | None = None,
        execution_router: Any | None = None,
    ) -> None:
        if config.dry_run is not True:
            raise RuntimeError("BacktestRunner requires config.dry_run=True")

        self.config = config
        self._data_loader = data_loader or BacktestDataLoader(config=config)
        self._data_aggregator = data_aggregator or self._build_data_aggregator()
        self._prompt_factory = prompt_factory or PromptFactory
        self._claude_client = claude_client or self._build_claude_client()
        self._execution_router = execution_router or self._build_execution_router()

    async def run(self) -> BacktestReport:
        started_at_utc = datetime.now(UTC)
        logger.info(
            "backtest.started",
            data_dir=self.config.data_dir,
            dry_run=self.config.dry_run,
        )

        try:
            loaded = self._data_loader.load_all()
            snapshots = await _maybe_await(loaded)
            snapshots = sorted(
                list(snapshots),
                key=lambda snapshot: _timestamp_field(snapshot),
            )

            decision_rows: list[BacktestDecision] = []
            market_accumulators: dict[str, dict[str, Decimal | int]] = {}
            executed_trade_pnls: list[Decimal] = []

            equity = _ZERO
            equity_peak = _ZERO
            max_drawdown_usdc = _ZERO

            for snapshot in snapshots:
                token_id = str(_snapshot_field(snapshot, "token_id"))
                timestamp_utc = _timestamp_field(snapshot)
                logger.info(
                    "backtest.market_loaded",
                    token_id=token_id,
                    timestamp_utc=timestamp_utc.isoformat(),
                )

                decision, trade_executed, trade_pnl = await self._replay_snapshot(
                    snapshot
                )
                decision_rows.append(decision)

                accumulator = market_accumulators.setdefault(
                    token_id,
                    {
                        "total_decisions": 0,
                        "trades_executed": 0,
                        "wins": 0,
                        "net_pnl_usdc": _ZERO,
                    },
                )
                accumulator["total_decisions"] = int(accumulator["total_decisions"]) + 1

                if trade_executed:
                    accumulator["trades_executed"] = (
                        int(accumulator["trades_executed"]) + 1
                    )
                    accumulator["net_pnl_usdc"] = (
                        _decimal_value(accumulator["net_pnl_usdc"]) + trade_pnl
                    )
                    if trade_pnl > _ZERO:
                        accumulator["wins"] = int(accumulator["wins"]) + 1

                    executed_trade_pnls.append(trade_pnl)
                    equity += trade_pnl
                    if equity > equity_peak:
                        equity_peak = equity
                    drawdown = equity_peak - equity
                    if drawdown > max_drawdown_usdc:
                        max_drawdown_usdc = drawdown

                logger.info(
                    "backtest.decision",
                    token_id=token_id,
                    timestamp_utc=timestamp_utc.isoformat(),
                    gatekeeper_result=decision.gatekeeper_result,
                    action=decision.action,
                )

            total_trades = len(executed_trade_pnls)
            winning_trades = sum(1 for pnl in executed_trade_pnls if pnl > _ZERO)
            net_pnl_usdc = sum(executed_trade_pnls, _ZERO)
            win_rate = _ratio(winning_trades, total_trades)
            sharpe_ratio = _compute_sharpe_ratio(executed_trade_pnls)

            per_market_stats: dict[str, BacktestMarketStats] = {}
            for token_id, accumulator in market_accumulators.items():
                trades_executed = int(accumulator["trades_executed"])
                wins = int(accumulator["wins"])
                per_market_stats[token_id] = BacktestMarketStats(
                    token_id=token_id,
                    total_decisions=int(accumulator["total_decisions"]),
                    trades_executed=trades_executed,
                    win_rate=_ratio(wins, trades_executed),
                    net_pnl_usdc=_decimal_value(accumulator["net_pnl_usdc"]),
                )

            completed_at_utc = datetime.now(UTC)
            report = BacktestReport(
                total_trades=total_trades,
                win_rate=win_rate,
                net_pnl_usdc=net_pnl_usdc,
                max_drawdown_usdc=max_drawdown_usdc,
                sharpe_ratio=sharpe_ratio,
                per_market_stats=per_market_stats,
                decisions=decision_rows,
                started_at_utc=started_at_utc,
                completed_at_utc=completed_at_utc,
                config_snapshot=self.config,
            )

            logger.info(
                "backtest.completed",
                total_snapshots=len(snapshots),
                total_trades=total_trades,
                win_rate=str(report.win_rate),
                net_pnl_usdc=str(report.net_pnl_usdc),
                max_drawdown_usdc=str(report.max_drawdown_usdc),
                sharpe_ratio=str(report.sharpe_ratio),
            )
            return report
        except Exception as exc:
            logger.error("backtest.error", error=str(exc))
            raise

    async def _replay_snapshot(
        self,
        snapshot: Any,
    ) -> tuple[BacktestDecision, bool, Decimal]:
        token_id = str(_snapshot_field(snapshot, "token_id"))
        timestamp_utc = _timestamp_field(snapshot)

        context = await _maybe_await(self._build_context(snapshot))
        prompt = await _maybe_await(self._build_prompt(context))
        raw_decision = await _maybe_await(self._claude_client.evaluate(prompt))
        validated = self._gatekeeper_validate(raw_decision)

        decision_boolean = bool(_safe_attr(validated, "decision_boolean", False))
        action = _action_value(_safe_attr(validated, "recommended_action", "HOLD"))
        position_size_pct = _to_decimal(
            _safe_attr(validated, "position_size_pct", _ZERO)
        )
        ev = _to_decimal(_safe_attr(validated, "expected_value", _ZERO))
        confidence = _to_decimal(_safe_attr(validated, "confidence_score", _ZERO))
        reason = str(_safe_attr(validated, "reasoning_log", ""))

        trade_executed = False
        trade_pnl = _ZERO

        if decision_boolean:
            trade_executed = True
            route_result = await self._route_execution(validated, snapshot)
            trade_pnl = _extract_realized_pnl(route_result, snapshot)
            gatekeeper_result = "PASSED"
        else:
            action = "HOLD"
            gatekeeper_result = "FAILED"

        decision = BacktestDecision(
            token_id=token_id,
            timestamp_utc=timestamp_utc,
            decision=decision_boolean,
            action=action,
            position_size_usdc=position_size_pct * self.config.initial_bankroll_usdc,
            ev=ev,
            confidence=confidence,
            gatekeeper_result=gatekeeper_result,
            reason=reason,
        )
        return decision, trade_executed, trade_pnl

    def _build_data_aggregator(self) -> Any:
        try:
            candidate = DataAggregator()  # type: ignore[call-arg]
            if hasattr(candidate, "build_context"):
                return candidate
        except TypeError:
            pass
        return _FallbackDataAggregator()

    def _build_claude_client(self) -> Any:
        try:
            candidate = ClaudeClient()  # type: ignore[call-arg]
            if hasattr(candidate, "evaluate"):
                return candidate
        except TypeError:
            pass
        return _FallbackClaudeClient()

    def _build_execution_router(self) -> Any:
        try:
            return ExecutionRouter()  # type: ignore[call-arg]
        except TypeError:
            return _FallbackExecutionRouter()

    def _build_context(self, snapshot: Any) -> Any:
        if hasattr(self._data_aggregator, "build_context"):
            return self._data_aggregator.build_context(snapshot)
        if hasattr(self._data_aggregator, "_build_market_context"):
            return self._data_aggregator._build_market_context(  # type: ignore[attr-defined]
                [str(_snapshot_field(snapshot, "token_id"))],
                SimpleNamespace(subscription_status="active", frame_count=0),
            )
        return _FallbackDataAggregator().build_context(snapshot)

    def _build_prompt(self, context: Any) -> Any:
        build_prompt = getattr(self._prompt_factory, "build_evaluation_prompt")
        try:
            return build_prompt(context)
        except TypeError:
            if isinstance(context, dict) and "state" in context:
                return build_prompt(context["state"])
            raise

    async def _route_execution(self, validated: Any, snapshot: Any) -> Any:
        route = getattr(self._execution_router, "route")
        market_context = _to_market_context(snapshot)

        try:
            result = route(
                response=validated,
                market_context=market_context,
                dry_run=True,
            )
            return await _maybe_await(result)
        except TypeError:
            pass

        try:
            result = route(validated, market_context, dry_run=True)
            return await _maybe_await(result)
        except TypeError:
            result = route(validated, market_context)
            return await _maybe_await(result)

    @staticmethod
    def _gatekeeper_validate(raw_decision: Any) -> Any:
        if isinstance(raw_decision, (dict, list)):
            raw_payload = json.dumps(raw_decision, default=str)
        else:
            raw_payload = raw_decision

        try:
            return LLMEvaluationResponse.model_validate_json(raw_payload)
        except Exception:
            model_validate = getattr(LLMEvaluationResponse, "model_validate", None)
            if model_validate is None:
                raise
            return model_validate(raw_decision)


def _snapshot_field(snapshot: Any, field_name: str) -> Any:
    if isinstance(snapshot, dict):
        return snapshot[field_name]
    return getattr(snapshot, field_name)


def _timestamp_field(snapshot: Any) -> datetime:
    value = _snapshot_field(snapshot, "timestamp_utc")
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    raise BacktestDataError(f"Unsupported timestamp type: {type(value)!r}")


def _decimal_field(snapshot: Any, field_name: str) -> Decimal:
    return _to_decimal(_snapshot_field(snapshot, field_name))


def _to_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        raise ValueError("Float values are forbidden in financial backtest paths")
    return Decimal(str(value))


def _ratio(numerator: int, denominator: int) -> Decimal:
    if denominator <= 0:
        return _ZERO
    return Decimal(numerator) / Decimal(denominator)


def _compute_sharpe_ratio(pnls: list[Decimal]) -> Decimal:
    count = len(pnls)
    if count < 2:
        return _ZERO

    total = sum(pnls, _ZERO)
    mean = total / Decimal(count)
    variance = sum((pnl - mean) ** 2 for pnl in pnls) / Decimal(count)
    if variance <= _ZERO:
        return _ZERO

    std_dev = variance.sqrt()
    if std_dev == _ZERO:
        return _ZERO
    return mean / std_dev


def _extract_realized_pnl(route_result: Any, snapshot: Any) -> Decimal:
    if hasattr(route_result, "realized_pnl_usdc"):
        return _to_decimal(getattr(route_result, "realized_pnl_usdc"))
    if isinstance(route_result, dict) and "realized_pnl_usdc" in route_result:
        return _to_decimal(route_result["realized_pnl_usdc"])
    if isinstance(snapshot, dict) and "realized_pnl_usdc" in snapshot:
        return _to_decimal(snapshot["realized_pnl_usdc"])
    if hasattr(snapshot, "realized_pnl_usdc"):
        return _to_decimal(getattr(snapshot, "realized_pnl_usdc"))
    return _ZERO


def _safe_attr(obj: Any, field_name: str, default: Any) -> Any:
    if isinstance(obj, dict):
        return obj.get(field_name, default)
    return getattr(obj, field_name, default)


def _action_value(action: Any) -> str:
    if hasattr(action, "value"):
        return str(action.value)
    return str(action)


def _decimal_value(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _to_market_context(snapshot: Any) -> SimpleNamespace:
    # Keep this adapter permissive for offline replay fixtures where token IDs
    # may not satisfy production MarketContext constraints.
    return SimpleNamespace(
        condition_id=str(_snapshot_field(snapshot, "token_id")),
        yes_token_id=str(_snapshot_field(snapshot, "token_id")),
        best_bid=float(_decimal_field(snapshot, "best_bid")),
        best_ask=float(_decimal_field(snapshot, "best_ask")),
        midpoint=float(_decimal_field(snapshot, "midpoint")),
    )


async def _maybe_await(value: Any) -> Any:
    if asyncio.iscoroutine(value) or isinstance(value, asyncio.Future):
        return await value
    return value


def _load_config_overrides(config_path: Path) -> dict[str, Any]:
    raw_text = config_path.read_text(encoding="utf-8")

    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        pass

    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError as exc:
        raise BacktestDataError(
            "Config file is not valid JSON and PyYAML is not installed for YAML parsing."
        ) from exc

    loaded = yaml.safe_load(raw_text)  # type: ignore[attr-defined]
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise BacktestDataError(
            "Backtest config file must deserialize to a dictionary."
        )
    return loaded


def _build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="WI-33 offline backtesting runner")
    parser.add_argument(
        "--data-dir", required=True, help="Directory of historical JSON files"
    )
    parser.add_argument(
        "--config", required=False, help="Optional backtest config (JSON or YAML)"
    )
    parser.add_argument(
        "--output",
        required=False,
        default="backtest_report.json",
        help="Output JSON report path",
    )
    return parser


async def _run_cli(argv: Sequence[str] | None = None) -> BacktestReport:
    args = _build_cli_parser().parse_args(list(argv) if argv is not None else None)

    config_payload: dict[str, Any] = {
        "data_dir": args.data_dir,
        "initial_bankroll_usdc": Decimal("1000"),
        "dry_run": True,
    }
    if args.config:
        config_payload.update(_load_config_overrides(Path(args.config)))
    config_payload["data_dir"] = args.data_dir
    config_payload["dry_run"] = True

    config = BacktestConfig(**config_payload)
    runner = BacktestRunner(config=config)
    report = await runner.run()

    output_path = Path(args.output)
    output_path.write_text(
        json.dumps(report.model_dump(mode="json"), indent=2),
        encoding="utf-8",
    )
    return report


def main(argv: Sequence[str] | None = None) -> BacktestReport | Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_run_cli(argv))
    return _run_cli(argv)


if __name__ == "__main__":
    main()
