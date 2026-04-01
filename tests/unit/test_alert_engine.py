"""
tests/unit/test_alert_engine.py

RED-phase unit tests for WI-25 AlertEngine.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import importlib
import inspect
from types import SimpleNamespace

import pytest

from src.core.config import AppConfig


ALERT_ENGINE_MODULE_NAME = "src.agents.execution.alert_engine"
SCHEMA_MODULE_NAME = "src.schemas.risk"


def _load_module(name: str):
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError:
        pytest.fail(f"Expected module {name} to exist.", pytrace=False)
    except Exception as exc:
        pytest.fail(f"Module {name} import failed unexpectedly: {exc!r}", pytrace=False)


def _build_engine(
    alert_module,
    *,
    alert_drawdown_usdc: Decimal = Decimal("100"),
    alert_stale_price_pct: Decimal = Decimal("0.50"),
    alert_max_open_positions: int = 20,
    alert_loss_rate_pct: Decimal = Decimal("0.60"),
):
    config = SimpleNamespace(
        alert_drawdown_usdc=alert_drawdown_usdc,
        alert_stale_price_pct=alert_stale_price_pct,
        alert_max_open_positions=alert_max_open_positions,
        alert_loss_rate_pct=alert_loss_rate_pct,
    )
    return alert_module.AlertEngine(config=config)


def _make_snapshot(
    schema_module,
    *,
    position_count: int,
    positions_with_stale_price: int,
    total_unrealized_pnl: Decimal,
    dry_run: bool,
):
    snapshot_cls = getattr(schema_module, "PortfolioSnapshot", None)
    assert snapshot_cls is not None

    return snapshot_cls(
        snapshot_at_utc=datetime.now(timezone.utc),
        position_count=position_count,
        total_notional_usdc=Decimal("100"),
        total_unrealized_pnl=total_unrealized_pnl,
        total_locked_collateral_usdc=Decimal("70"),
        positions_with_stale_price=positions_with_stale_price,
        dry_run=dry_run,
    )


def _make_report(
    schema_module,
    *,
    total_settled_count: int,
    losing_count: int,
    winning_count: int,
    dry_run: bool,
):
    report_cls = getattr(schema_module, "LifecycleReport", None)
    assert report_cls is not None

    breakeven_count = total_settled_count - losing_count - winning_count
    assert breakeven_count >= 0

    return report_cls(
        report_at_utc=datetime.now(timezone.utc),
        total_settled_count=total_settled_count,
        winning_count=winning_count,
        losing_count=losing_count,
        breakeven_count=breakeven_count,
        total_realized_pnl=Decimal("0"),
        avg_hold_duration_hours=Decimal("0"),
        best_pnl=Decimal("0"),
        worst_pnl=Decimal("0"),
        entries=[],
        dry_run=dry_run,
    )


def _find_rule(alerts, rule_name: str):
    return [alert for alert in alerts if alert.rule_name == rule_name]


def test_alert_severity_enum_exists_with_expected_members():
    schema_module = _load_module(SCHEMA_MODULE_NAME)
    severity_cls = getattr(schema_module, "AlertSeverity", None)

    assert severity_cls is not None
    assert [member.name for member in severity_cls] == [
        "INFO",
        "WARNING",
        "CRITICAL",
    ]
    assert [member.value for member in severity_cls] == [
        "INFO",
        "WARNING",
        "CRITICAL",
    ]


def test_alert_event_schema_exists_and_is_frozen():
    schema_module = _load_module(SCHEMA_MODULE_NAME)
    severity_cls = getattr(schema_module, "AlertSeverity", None)
    event_cls = getattr(schema_module, "AlertEvent", None)

    assert severity_cls is not None
    assert event_cls is not None
    assert {
        "alert_at_utc",
        "severity",
        "rule_name",
        "message",
        "threshold_value",
        "actual_value",
        "dry_run",
    }.issubset(event_cls.model_fields.keys())

    event = event_cls(
        alert_at_utc=datetime.now(timezone.utc),
        severity=severity_cls.WARNING,
        rule_name="stale_price",
        message="Stale ratio exceeded",
        threshold_value=Decimal("0.50"),
        actual_value=Decimal("0.75"),
        dry_run=True,
    )

    with pytest.raises(Exception):
        event.message = "mutated"


@pytest.mark.parametrize("field_name", ["threshold_value", "actual_value"])
def test_alert_event_rejects_float_financial_fields(field_name):
    schema_module = _load_module(SCHEMA_MODULE_NAME)
    severity_cls = getattr(schema_module, "AlertSeverity", None)
    event_cls = getattr(schema_module, "AlertEvent", None)

    assert severity_cls is not None
    assert event_cls is not None

    payload = {
        "alert_at_utc": datetime.now(timezone.utc),
        "severity": severity_cls.WARNING,
        "rule_name": "loss_rate",
        "message": "Loss rate exceeded",
        "threshold_value": Decimal("0.60"),
        "actual_value": Decimal("0.70"),
        "dry_run": False,
    }
    payload[field_name] = 0.1

    with pytest.raises(Exception):
        event_cls(**payload)


def test_alert_event_accepts_alert_severity_enum_values():
    schema_module = _load_module(SCHEMA_MODULE_NAME)
    severity_cls = getattr(schema_module, "AlertSeverity", None)
    event_cls = getattr(schema_module, "AlertEvent", None)

    assert severity_cls is not None
    assert event_cls is not None

    event = event_cls(
        alert_at_utc=datetime.now(timezone.utc),
        severity=severity_cls.CRITICAL,
        rule_name="drawdown",
        message="Drawdown exceeded",
        threshold_value=Decimal("100"),
        actual_value=Decimal("-150"),
        dry_run=True,
    )

    assert event.severity == severity_cls.CRITICAL


def test_alert_engine_contract_exists_with_single_public_sync_method():
    alert_module = _load_module(ALERT_ENGINE_MODULE_NAME)
    alert_cls = getattr(alert_module, "AlertEngine", None)

    assert alert_cls is not None
    assert inspect.isclass(alert_cls)
    assert hasattr(alert_cls, "evaluate")
    assert not inspect.iscoroutinefunction(alert_cls.evaluate)

    init_params = list(inspect.signature(alert_cls.__init__).parameters.keys())
    assert init_params == ["self", "config"]

    public_methods = [
        name
        for name, member in inspect.getmembers(alert_cls, predicate=inspect.isfunction)
        if not name.startswith("_")
    ]
    assert public_methods == ["evaluate"]


def test_evaluate_drawdown_rule_fires_critical_when_below_negative_threshold():
    schema_module = _load_module(SCHEMA_MODULE_NAME)
    alert_module = _load_module(ALERT_ENGINE_MODULE_NAME)
    severity_cls = getattr(schema_module, "AlertSeverity", None)
    assert severity_cls is not None

    engine = _build_engine(alert_module, alert_drawdown_usdc=Decimal("100"))
    snapshot = _make_snapshot(
        schema_module,
        position_count=0,
        positions_with_stale_price=0,
        total_unrealized_pnl=Decimal("-100.01"),
        dry_run=True,
    )
    report = _make_report(
        schema_module,
        total_settled_count=0,
        losing_count=0,
        winning_count=0,
        dry_run=True,
    )

    alerts = engine.evaluate(snapshot, report)
    drawdown_alerts = _find_rule(alerts, "drawdown")

    assert len(drawdown_alerts) == 1
    drawdown = drawdown_alerts[0]
    assert drawdown.severity == severity_cls.CRITICAL
    assert drawdown.threshold_value == Decimal("100")
    assert drawdown.actual_value == Decimal("-100.01")


def test_evaluate_drawdown_rule_does_not_fire_at_boundary_equal_threshold():
    schema_module = _load_module(SCHEMA_MODULE_NAME)
    alert_module = _load_module(ALERT_ENGINE_MODULE_NAME)

    engine = _build_engine(alert_module, alert_drawdown_usdc=Decimal("100"))
    snapshot = _make_snapshot(
        schema_module,
        position_count=0,
        positions_with_stale_price=0,
        total_unrealized_pnl=Decimal("-100"),
        dry_run=True,
    )
    report = _make_report(
        schema_module,
        total_settled_count=0,
        losing_count=0,
        winning_count=0,
        dry_run=True,
    )

    alerts = engine.evaluate(snapshot, report)

    assert _find_rule(alerts, "drawdown") == []


def test_evaluate_drawdown_rule_does_not_fire_when_above_negative_threshold():
    schema_module = _load_module(SCHEMA_MODULE_NAME)
    alert_module = _load_module(ALERT_ENGINE_MODULE_NAME)

    engine = _build_engine(alert_module, alert_drawdown_usdc=Decimal("100"))
    snapshot = _make_snapshot(
        schema_module,
        position_count=0,
        positions_with_stale_price=0,
        total_unrealized_pnl=Decimal("-99.99"),
        dry_run=True,
    )
    report = _make_report(
        schema_module,
        total_settled_count=0,
        losing_count=0,
        winning_count=0,
        dry_run=True,
    )

    alerts = engine.evaluate(snapshot, report)

    assert _find_rule(alerts, "drawdown") == []


def test_evaluate_drawdown_alert_has_expected_rule_name_and_severity():
    schema_module = _load_module(SCHEMA_MODULE_NAME)
    alert_module = _load_module(ALERT_ENGINE_MODULE_NAME)
    severity_cls = getattr(schema_module, "AlertSeverity", None)
    assert severity_cls is not None

    engine = _build_engine(alert_module)
    snapshot = _make_snapshot(
        schema_module,
        position_count=1,
        positions_with_stale_price=0,
        total_unrealized_pnl=Decimal("-250"),
        dry_run=True,
    )
    report = _make_report(
        schema_module,
        total_settled_count=1,
        losing_count=0,
        winning_count=1,
        dry_run=True,
    )

    alert = _find_rule(engine.evaluate(snapshot, report), "drawdown")[0]

    assert alert.rule_name == "drawdown"
    assert alert.severity == severity_cls.CRITICAL


def test_evaluate_stale_price_rule_fires_when_ratio_exceeds_threshold():
    schema_module = _load_module(SCHEMA_MODULE_NAME)
    alert_module = _load_module(ALERT_ENGINE_MODULE_NAME)

    engine = _build_engine(alert_module, alert_stale_price_pct=Decimal("0.50"))
    snapshot = _make_snapshot(
        schema_module,
        position_count=4,
        positions_with_stale_price=3,
        total_unrealized_pnl=Decimal("0"),
        dry_run=True,
    )
    report = _make_report(
        schema_module,
        total_settled_count=1,
        losing_count=0,
        winning_count=1,
        dry_run=True,
    )

    alerts = engine.evaluate(snapshot, report)

    stale_alerts = _find_rule(alerts, "stale_price")
    assert len(stale_alerts) == 1
    assert stale_alerts[0].actual_value == Decimal("0.75")


def test_evaluate_stale_price_rule_does_not_fire_at_boundary_equal_threshold():
    schema_module = _load_module(SCHEMA_MODULE_NAME)
    alert_module = _load_module(ALERT_ENGINE_MODULE_NAME)

    engine = _build_engine(alert_module, alert_stale_price_pct=Decimal("0.50"))
    snapshot = _make_snapshot(
        schema_module,
        position_count=4,
        positions_with_stale_price=2,
        total_unrealized_pnl=Decimal("0"),
        dry_run=True,
    )
    report = _make_report(
        schema_module,
        total_settled_count=2,
        losing_count=1,
        winning_count=1,
        dry_run=True,
    )

    alerts = engine.evaluate(snapshot, report)

    assert _find_rule(alerts, "stale_price") == []


def test_evaluate_stale_price_rule_skips_when_position_count_is_zero():
    schema_module = _load_module(SCHEMA_MODULE_NAME)
    alert_module = _load_module(ALERT_ENGINE_MODULE_NAME)

    engine = _build_engine(alert_module, alert_stale_price_pct=Decimal("0.01"))
    snapshot = _make_snapshot(
        schema_module,
        position_count=0,
        positions_with_stale_price=999,
        total_unrealized_pnl=Decimal("0"),
        dry_run=True,
    )
    report = _make_report(
        schema_module,
        total_settled_count=4,
        losing_count=1,
        winning_count=3,
        dry_run=True,
    )

    alerts = engine.evaluate(snapshot, report)

    assert _find_rule(alerts, "stale_price") == []


def test_evaluate_stale_price_alert_has_expected_rule_name_and_severity():
    schema_module = _load_module(SCHEMA_MODULE_NAME)
    alert_module = _load_module(ALERT_ENGINE_MODULE_NAME)
    severity_cls = getattr(schema_module, "AlertSeverity", None)
    assert severity_cls is not None

    engine = _build_engine(alert_module)
    snapshot = _make_snapshot(
        schema_module,
        position_count=10,
        positions_with_stale_price=9,
        total_unrealized_pnl=Decimal("0"),
        dry_run=True,
    )
    report = _make_report(
        schema_module,
        total_settled_count=1,
        losing_count=0,
        winning_count=1,
        dry_run=True,
    )

    alert = _find_rule(engine.evaluate(snapshot, report), "stale_price")[0]

    assert alert.rule_name == "stale_price"
    assert alert.severity == severity_cls.WARNING


def test_evaluate_max_positions_rule_fires_when_position_count_exceeds_threshold():
    schema_module = _load_module(SCHEMA_MODULE_NAME)
    alert_module = _load_module(ALERT_ENGINE_MODULE_NAME)

    engine = _build_engine(alert_module, alert_max_open_positions=20)
    snapshot = _make_snapshot(
        schema_module,
        position_count=21,
        positions_with_stale_price=0,
        total_unrealized_pnl=Decimal("0"),
        dry_run=True,
    )
    report = _make_report(
        schema_module,
        total_settled_count=10,
        losing_count=1,
        winning_count=9,
        dry_run=True,
    )

    alerts = engine.evaluate(snapshot, report)

    assert len(_find_rule(alerts, "max_positions")) == 1


def test_evaluate_max_positions_rule_does_not_fire_at_boundary_equal_threshold():
    schema_module = _load_module(SCHEMA_MODULE_NAME)
    alert_module = _load_module(ALERT_ENGINE_MODULE_NAME)

    engine = _build_engine(alert_module, alert_max_open_positions=20)
    snapshot = _make_snapshot(
        schema_module,
        position_count=20,
        positions_with_stale_price=0,
        total_unrealized_pnl=Decimal("0"),
        dry_run=True,
    )
    report = _make_report(
        schema_module,
        total_settled_count=10,
        losing_count=1,
        winning_count=9,
        dry_run=True,
    )

    alerts = engine.evaluate(snapshot, report)

    assert _find_rule(alerts, "max_positions") == []


def test_evaluate_max_positions_rule_does_not_fire_when_below_threshold():
    schema_module = _load_module(SCHEMA_MODULE_NAME)
    alert_module = _load_module(ALERT_ENGINE_MODULE_NAME)

    engine = _build_engine(alert_module, alert_max_open_positions=20)
    snapshot = _make_snapshot(
        schema_module,
        position_count=19,
        positions_with_stale_price=0,
        total_unrealized_pnl=Decimal("0"),
        dry_run=True,
    )
    report = _make_report(
        schema_module,
        total_settled_count=10,
        losing_count=1,
        winning_count=9,
        dry_run=True,
    )

    alerts = engine.evaluate(snapshot, report)

    assert _find_rule(alerts, "max_positions") == []


def test_evaluate_max_positions_alert_has_expected_rule_name_and_severity():
    schema_module = _load_module(SCHEMA_MODULE_NAME)
    alert_module = _load_module(ALERT_ENGINE_MODULE_NAME)
    severity_cls = getattr(schema_module, "AlertSeverity", None)
    assert severity_cls is not None

    engine = _build_engine(alert_module, alert_max_open_positions=5)
    snapshot = _make_snapshot(
        schema_module,
        position_count=8,
        positions_with_stale_price=0,
        total_unrealized_pnl=Decimal("0"),
        dry_run=True,
    )
    report = _make_report(
        schema_module,
        total_settled_count=1,
        losing_count=0,
        winning_count=1,
        dry_run=True,
    )

    alert = _find_rule(engine.evaluate(snapshot, report), "max_positions")[0]

    assert alert.rule_name == "max_positions"
    assert alert.severity == severity_cls.WARNING


def test_evaluate_max_positions_alert_uses_decimal_threshold_and_actual_values():
    schema_module = _load_module(SCHEMA_MODULE_NAME)
    alert_module = _load_module(ALERT_ENGINE_MODULE_NAME)

    engine = _build_engine(alert_module, alert_max_open_positions=3)
    snapshot = _make_snapshot(
        schema_module,
        position_count=9,
        positions_with_stale_price=0,
        total_unrealized_pnl=Decimal("0"),
        dry_run=True,
    )
    report = _make_report(
        schema_module,
        total_settled_count=1,
        losing_count=0,
        winning_count=1,
        dry_run=True,
    )

    alert = _find_rule(engine.evaluate(snapshot, report), "max_positions")[0]

    assert isinstance(alert.threshold_value, Decimal)
    assert isinstance(alert.actual_value, Decimal)
    assert alert.threshold_value == Decimal("3")
    assert alert.actual_value == Decimal("9")


def test_evaluate_loss_rate_rule_fires_when_ratio_exceeds_threshold():
    schema_module = _load_module(SCHEMA_MODULE_NAME)
    alert_module = _load_module(ALERT_ENGINE_MODULE_NAME)

    engine = _build_engine(alert_module, alert_loss_rate_pct=Decimal("0.60"))
    snapshot = _make_snapshot(
        schema_module,
        position_count=1,
        positions_with_stale_price=0,
        total_unrealized_pnl=Decimal("0"),
        dry_run=True,
    )
    report = _make_report(
        schema_module,
        total_settled_count=10,
        losing_count=7,
        winning_count=3,
        dry_run=True,
    )

    alerts = engine.evaluate(snapshot, report)

    loss_alerts = _find_rule(alerts, "loss_rate")
    assert len(loss_alerts) == 1
    assert loss_alerts[0].actual_value == Decimal("0.7")


def test_evaluate_loss_rate_rule_does_not_fire_at_boundary_equal_threshold():
    schema_module = _load_module(SCHEMA_MODULE_NAME)
    alert_module = _load_module(ALERT_ENGINE_MODULE_NAME)

    engine = _build_engine(alert_module, alert_loss_rate_pct=Decimal("0.60"))
    snapshot = _make_snapshot(
        schema_module,
        position_count=1,
        positions_with_stale_price=0,
        total_unrealized_pnl=Decimal("0"),
        dry_run=True,
    )
    report = _make_report(
        schema_module,
        total_settled_count=10,
        losing_count=6,
        winning_count=4,
        dry_run=True,
    )

    alerts = engine.evaluate(snapshot, report)

    assert _find_rule(alerts, "loss_rate") == []


def test_evaluate_loss_rate_rule_skips_when_total_settled_count_is_zero():
    schema_module = _load_module(SCHEMA_MODULE_NAME)
    alert_module = _load_module(ALERT_ENGINE_MODULE_NAME)

    engine = _build_engine(alert_module, alert_loss_rate_pct=Decimal("0.01"))
    snapshot = _make_snapshot(
        schema_module,
        position_count=1,
        positions_with_stale_price=0,
        total_unrealized_pnl=Decimal("0"),
        dry_run=True,
    )
    report = _make_report(
        schema_module,
        total_settled_count=0,
        losing_count=0,
        winning_count=0,
        dry_run=True,
    )

    alerts = engine.evaluate(snapshot, report)

    assert _find_rule(alerts, "loss_rate") == []


def test_evaluate_loss_rate_alert_has_expected_rule_name_and_severity():
    schema_module = _load_module(SCHEMA_MODULE_NAME)
    alert_module = _load_module(ALERT_ENGINE_MODULE_NAME)
    severity_cls = getattr(schema_module, "AlertSeverity", None)
    assert severity_cls is not None

    engine = _build_engine(alert_module, alert_loss_rate_pct=Decimal("0.40"))
    snapshot = _make_snapshot(
        schema_module,
        position_count=1,
        positions_with_stale_price=0,
        total_unrealized_pnl=Decimal("0"),
        dry_run=True,
    )
    report = _make_report(
        schema_module,
        total_settled_count=10,
        losing_count=8,
        winning_count=2,
        dry_run=True,
    )

    alert = _find_rule(engine.evaluate(snapshot, report), "loss_rate")[0]

    assert alert.rule_name == "loss_rate"
    assert alert.severity == severity_cls.WARNING


def test_evaluate_returns_empty_list_when_no_rules_fire():
    schema_module = _load_module(SCHEMA_MODULE_NAME)
    alert_module = _load_module(ALERT_ENGINE_MODULE_NAME)

    engine = _build_engine(alert_module)
    snapshot = _make_snapshot(
        schema_module,
        position_count=3,
        positions_with_stale_price=1,
        total_unrealized_pnl=Decimal("-10"),
        dry_run=True,
    )
    report = _make_report(
        schema_module,
        total_settled_count=10,
        losing_count=4,
        winning_count=6,
        dry_run=True,
    )

    alerts = engine.evaluate(snapshot, report)

    assert alerts == []


def test_evaluate_returns_multiple_alerts_when_multiple_rules_fire():
    schema_module = _load_module(SCHEMA_MODULE_NAME)
    alert_module = _load_module(ALERT_ENGINE_MODULE_NAME)

    engine = _build_engine(alert_module, alert_max_open_positions=5)
    snapshot = _make_snapshot(
        schema_module,
        position_count=8,
        positions_with_stale_price=0,
        total_unrealized_pnl=Decimal("-150"),
        dry_run=True,
    )
    report = _make_report(
        schema_module,
        total_settled_count=10,
        losing_count=2,
        winning_count=8,
        dry_run=True,
    )

    alerts = engine.evaluate(snapshot, report)
    rules = [alert.rule_name for alert in alerts]

    assert "drawdown" in rules
    assert "max_positions" in rules
    assert len(alerts) == 2


def test_evaluate_returns_all_four_alerts_when_all_thresholds_breached():
    schema_module = _load_module(SCHEMA_MODULE_NAME)
    alert_module = _load_module(ALERT_ENGINE_MODULE_NAME)

    engine = _build_engine(
        alert_module,
        alert_drawdown_usdc=Decimal("100"),
        alert_stale_price_pct=Decimal("0.10"),
        alert_max_open_positions=2,
        alert_loss_rate_pct=Decimal("0.10"),
    )
    snapshot = _make_snapshot(
        schema_module,
        position_count=10,
        positions_with_stale_price=9,
        total_unrealized_pnl=Decimal("-250"),
        dry_run=True,
    )
    report = _make_report(
        schema_module,
        total_settled_count=10,
        losing_count=9,
        winning_count=1,
        dry_run=True,
    )

    alerts = engine.evaluate(snapshot, report)

    assert [alert.rule_name for alert in alerts] == [
        "drawdown",
        "stale_price",
        "max_positions",
        "loss_rate",
    ]


def test_evaluate_propagates_snapshot_dry_run_true_to_all_alerts():
    schema_module = _load_module(SCHEMA_MODULE_NAME)
    alert_module = _load_module(ALERT_ENGINE_MODULE_NAME)

    engine = _build_engine(
        alert_module,
        alert_stale_price_pct=Decimal("0.01"),
        alert_max_open_positions=1,
        alert_loss_rate_pct=Decimal("0.01"),
    )
    snapshot = _make_snapshot(
        schema_module,
        position_count=5,
        positions_with_stale_price=5,
        total_unrealized_pnl=Decimal("-999"),
        dry_run=True,
    )
    report = _make_report(
        schema_module,
        total_settled_count=10,
        losing_count=10,
        winning_count=0,
        dry_run=True,
    )

    alerts = engine.evaluate(snapshot, report)

    assert alerts
    assert all(alert.dry_run is True for alert in alerts)


def test_evaluate_propagates_snapshot_dry_run_false_to_all_alerts():
    schema_module = _load_module(SCHEMA_MODULE_NAME)
    alert_module = _load_module(ALERT_ENGINE_MODULE_NAME)

    engine = _build_engine(
        alert_module,
        alert_stale_price_pct=Decimal("0.01"),
        alert_max_open_positions=1,
        alert_loss_rate_pct=Decimal("0.01"),
    )
    snapshot = _make_snapshot(
        schema_module,
        position_count=5,
        positions_with_stale_price=5,
        total_unrealized_pnl=Decimal("-999"),
        dry_run=False,
    )
    report = _make_report(
        schema_module,
        total_settled_count=10,
        losing_count=10,
        winning_count=0,
        dry_run=False,
    )

    alerts = engine.evaluate(snapshot, report)

    assert alerts
    assert all(alert.dry_run is False for alert in alerts)


def test_app_config_has_alert_drawdown_decimal_default_100():
    field = AppConfig.model_fields["alert_drawdown_usdc"]
    assert field.annotation is Decimal
    assert field.default == Decimal("100")


def test_app_config_has_alert_stale_price_pct_decimal_default_point_50():
    field = AppConfig.model_fields["alert_stale_price_pct"]
    assert field.annotation is Decimal
    assert field.default == Decimal("0.50")


def test_app_config_has_alert_max_open_positions_int_default_20():
    field = AppConfig.model_fields["alert_max_open_positions"]
    assert field.annotation is int
    assert field.default == 20


def test_app_config_has_alert_loss_rate_pct_decimal_default_point_60():
    field = AppConfig.model_fields["alert_loss_rate_pct"]
    assert field.annotation is Decimal
    assert field.default == Decimal("0.60")


def test_app_config_accepts_alert_threshold_overrides_from_env(monkeypatch):
    monkeypatch.setenv("ALERT_DRAWDOWN_USDC", "250")
    monkeypatch.setenv("ALERT_STALE_PRICE_PCT", "0.25")
    monkeypatch.setenv("ALERT_MAX_OPEN_POSITIONS", "7")
    monkeypatch.setenv("ALERT_LOSS_RATE_PCT", "0.33")

    cfg = AppConfig()

    assert cfg.alert_drawdown_usdc == Decimal("250")
    assert cfg.alert_stale_price_pct == Decimal("0.25")
    assert cfg.alert_max_open_positions == 7
    assert cfg.alert_loss_rate_pct == Decimal("0.33")
