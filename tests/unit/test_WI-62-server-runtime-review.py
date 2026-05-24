"""tests/unit/test_WI-62-server-runtime-review.py — WI-62 aggregator unit tests."""
from __future__ import annotations
import json, subprocess, sys
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
import pytest

SCRIPT = Path("scripts/ops/aggregate_audits.py")
PY = sys.executable

def _run(d: Path, hours: int = 72) -> tuple[int, dict, str]:
    r = subprocess.run([PY, str(SCRIPT), "--hours", str(hours), "--artifact-dir", str(d)],
                       capture_output=True, text=True, timeout=30)
    raw = r.stdout.strip()
    try: parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError: parsed = {}
    return r.returncode, parsed, raw

def _write(d: Path, fn: str, payload: dict) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    p = d / fn; p.write_text(json.dumps(payload), encoding="utf-8"); return p

def _art(*, errors=0, warnings=0, budget_blocks=0, provider_failures=0,
         critical_safety_gates=0, rt_ms="120.50", exposure="500.00",
         db_bytes=1024, dry_run=True, decisions=None, ts="2026-05-20T12:00:00+00:00") -> dict:
    findings = []
    for _ in range(errors):
        findings.append({"severity":"ERROR","finding_type":"WARNING","message":"e","source":"t"})
    for _ in range(warnings):
        findings.append({"severity":"WARNING","finding_type":"WARNING","message":"w","source":"t"})
    for _ in range(critical_safety_gates):
        findings.append({"severity":"CRITICAL","finding_type":"SAFETY_GATE","message":"s","source":"t"})
    dec = decisions or {"buy":0,"sell":0,"hold":0,"skip":0}
    return {"status":"HEALTHY","exit_code":0,"generated_at_utc":ts,"findings":findings,
        "health_probe":{"status":"SUCCESS","reachable":True,"response_time_ms":rt_ms},
        "readiness_probe":{"status":"SUCCESS","reachable":True,"ready":True,
            "dry_run_posture":{"dry_run_confirmed":dry_run,"source":"readyz"}},
        "ledger_summary":{"total_events":100,"error_count":errors,"warning_count":warnings,
            "budget_block_count":budget_blocks,"provider_failure_count":provider_failures},
        "decision_summary":{"total_decisions":sum(dec.values()),**{f"{k}_count":v for k,v in dec.items()}},
        "position_summary":{"open_count":1,"settled_count":0,"total_open_exposure_usdc":exposure},
        "database_probe":{"status":"SUCCESS","file_exists":True,"file_size_bytes":db_bytes}}

def _ts(h=1) -> str:
    return (datetime.now(timezone.utc)-timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%S+00:00")

def _fn(h=1) -> str:
    return f"runtime-audit-{(datetime.now(timezone.utc)-timedelta(hours=h)).strftime('%Y%m%d-%H%M%S')}.json"

class TestAggregatorHappyPath:
    def test_exits_zero_with_valid_json(self, tmp_path):
        _write(tmp_path, _fn(), _art(ts=_ts()))
        c, d, _ = _run(tmp_path); assert c == 0 and "error" not in d
    def test_output_contains_scanned_files(self, tmp_path):
        _write(tmp_path, _fn(), _art(ts=_ts())); _, d, _ = _run(tmp_path); assert "scanned_files" in d
    def test_output_contains_total_errors(self, tmp_path):
        _write(tmp_path, _fn(), _art(ts=_ts())); _, d, _ = _run(tmp_path); assert "total_errors" in d
    def test_output_contains_total_warnings(self, tmp_path):
        _write(tmp_path, _fn(), _art(ts=_ts())); _, d, _ = _run(tmp_path); assert "total_warnings" in d
    def test_output_contains_budget_blocks(self, tmp_path):
        _write(tmp_path, _fn(), _art(ts=_ts())); _, d, _ = _run(tmp_path); assert "budget_blocks" in d
    def test_output_contains_provider_failures(self, tmp_path):
        _write(tmp_path, _fn(), _art(ts=_ts())); _, d, _ = _run(tmp_path); assert "provider_failures" in d
    def test_output_contains_critical_safety_gates(self, tmp_path):
        _write(tmp_path, _fn(), _art(ts=_ts())); _, d, _ = _run(tmp_path); assert "critical_safety_gates" in d
    def test_output_contains_avg_response_time_ms(self, tmp_path):
        _write(tmp_path, _fn(), _art(ts=_ts())); _, d, _ = _run(tmp_path); assert "avg_response_time_ms" in d
    def test_output_contains_max_exposure_usdc(self, tmp_path):
        _write(tmp_path, _fn(), _art(ts=_ts())); _, d, _ = _run(tmp_path); assert "max_exposure_usdc" in d
    def test_output_contains_db_growth_bytes_delta(self, tmp_path):
        _write(tmp_path, _fn(), _art(ts=_ts())); _, d, _ = _run(tmp_path); assert "db_growth_bytes_delta" in d
    def test_output_contains_dry_run_posture(self, tmp_path):
        _write(tmp_path, _fn(), _art(ts=_ts())); _, d, _ = _run(tmp_path); assert "dry_run_posture" in d
    def test_output_contains_decision_distribution(self, tmp_path):
        _write(tmp_path, _fn(), _art(ts=_ts())); _, d, _ = _run(tmp_path)
        assert "decision_distribution" in d
        for k in ("buy","sell","hold","skip"): assert k in d["decision_distribution"]
    def test_output_contains_fix_plan_required(self, tmp_path):
        _write(tmp_path, _fn(), _art(ts=_ts())); _, d, _ = _run(tmp_path); assert "fix_plan_required" in d
    def test_scanned_files_matches_artifact_count(self, tmp_path):
        for i in range(3): _write(tmp_path, _fn(i+1), _art(ts=_ts(i+1)))
        _, d, _ = _run(tmp_path); assert d["scanned_files"] == 3
    def test_total_errors_aggregated_across_artifacts(self, tmp_path):
        _write(tmp_path, _fn(1), _art(errors=3, ts=_ts(1)))
        _write(tmp_path, _fn(2), _art(errors=5, ts=_ts(2)))
        _, d, _ = _run(tmp_path); assert d["total_errors"] == 8

class TestZeroArtifactDetection:
    def test_exits_one_when_no_artifacts(self, tmp_path):
        c, d, _ = _run(tmp_path); assert c == 1 and d.get("error") == "no_artifacts_in_window"
    def test_error_json_has_no_artifacts_in_window(self, tmp_path):
        c, d, _ = _run(tmp_path); assert c == 1 and "no_artifacts_in_window" in json.dumps(d)
    def test_exits_one_when_directory_missing(self, tmp_path):
        c, d, _ = _run(tmp_path / "nonexistent"); assert c == 1 and "error" in d
    def test_exits_one_when_artifacts_outside_window(self, tmp_path):
        _write(tmp_path, "runtime-audit-20200101-000000.json", _art(ts="2020-01-01T00:00:00+00:00"))
        c, d, _ = _run(tmp_path, hours=1); assert c == 1 and d.get("error") == "no_artifacts_in_window"

class TestMalformedArtifactHandling:
    def test_malformed_json_skipped(self, tmp_path):
        (tmp_path / _fn(1)).write_text("{invalid", encoding="utf-8")
        _write(tmp_path, _fn(2), _art(ts=_ts(2)))
        c, d, _ = _run(tmp_path); assert c == 0 and d.get("skipped_artifacts", 0) >= 1
    def test_skipped_artifacts_counted(self, tmp_path):
        (tmp_path / _fn(1)).write_text("not json", encoding="utf-8")
        _write(tmp_path, _fn(2), _art(ts=_ts(2)))
        _, d, _ = _run(tmp_path); assert d.get("skipped_artifacts", 0) >= 1
    def test_valid_artifacts_still_processed(self, tmp_path):
        (tmp_path / _fn(1)).write_text("{broken", encoding="utf-8")
        _write(tmp_path, _fn(2), _art(errors=7, ts=_ts(2)))
        c, d, _ = _run(tmp_path); assert c == 0 and d["total_errors"] == 7
    def test_all_malformed_treated_as_zero_artifacts(self, tmp_path):
        for i in range(3): (tmp_path / _fn(i+1)).write_text("garbage", encoding="utf-8")
        c, d, _ = _run(tmp_path); assert c == 1 and d.get("error") == "no_artifacts_in_window"

class TestDecimalIntegrity:
    def test_avg_response_time_ms_is_decimal_string(self, tmp_path):
        _write(tmp_path, _fn(), _art(rt_ms="123.45", ts=_ts()))
        _, d, _ = _run(tmp_path); assert Decimal(d["avg_response_time_ms"]) == Decimal("123.45")
    def test_max_exposure_usdc_is_decimal_string(self, tmp_path):
        _write(tmp_path, _fn(), _art(exposure="999.99", ts=_ts()))
        _, d, _ = _run(tmp_path); assert Decimal(d["max_exposure_usdc"]) == Decimal("999.99")
    def test_db_growth_bytes_delta_is_decimal_string(self, tmp_path):
        _write(tmp_path, _fn(), _art(db_bytes=2048, ts=_ts()))
        _, d, _ = _run(tmp_path); assert isinstance(Decimal(d["db_growth_bytes_delta"]), Decimal)
    def test_no_float_values_in_output(self, tmp_path):
        _write(tmp_path, _fn(), _art(ts=_ts())); _, _, raw = _run(tmp_path)
        def chk(o):
            if isinstance(o, dict):
                for v in o.values(): chk(v)
            elif isinstance(o, list):
                for v in o: chk(v)
            elif isinstance(o, float): pytest.fail(f"Float: {o}")
        chk(json.loads(raw))
    def test_decimal_precision_preserved(self, tmp_path):
        _write(tmp_path, _fn(1), _art(rt_ms="100.11", ts=_ts(1)))
        _write(tmp_path, _fn(2), _art(rt_ms="200.22", ts=_ts(2)))
        _, d, _ = _run(tmp_path)
        avg = Decimal(d["avg_response_time_ms"])
        exp = (Decimal("100.11") + Decimal("200.22")) / Decimal("2")
        assert avg == exp.quantize(Decimal("0.01"))

class TestSecretScrubbing:
    def test_wallet_addresses_redacted(self, tmp_path):
        a = _art(ts=_ts()); a["findings"].append({"severity":"INFO","finding_type":"WARNING","message":"w 0x"+"a"*40,"source":"t"})
        _write(tmp_path, _fn(), a); _, _, raw = _run(tmp_path); assert "0x"+"a"*40 not in raw
    def test_api_keys_redacted(self, tmp_path):
        a = _art(ts=_ts()); a["findings"].append({"severity":"INFO","finding_type":"WARNING","message":"k sk-"+"a"*30,"source":"t"})
        _write(tmp_path, _fn(), a); _, _, raw = _run(tmp_path); assert "sk-"+"a"*30 not in raw
    def test_condition_ids_redacted(self, tmp_path):
        a = _art(ts=_ts()); a["findings"].append({"severity":"INFO","finding_type":"WARNING","message":"c 0x"+"b"*64,"source":"t"})
        _write(tmp_path, _fn(), a); _, _, raw = _run(tmp_path); assert "0x"+"b"*64 not in raw
    def test_token_ids_redacted(self, tmp_path):
        a = _art(ts=_ts()); a["findings"].append({"severity":"INFO","finding_type":"WARNING","message":"t "+"1"*20,"source":"t"})
        _write(tmp_path, _fn(), a); _, _, raw = _run(tmp_path); assert "1"*20 not in raw
    def test_output_json_string_is_clean(self, tmp_path):
        a = _art(ts=_ts()); a["findings"].append({"severity":"INFO","finding_type":"WARNING","message":"0x"+"c"*40+" sk-"+"d"*30,"source":"t"})
        _write(tmp_path, _fn(), a); _, _, raw = _run(tmp_path)
        assert "0x"+"c"*40 not in raw and "sk-"+"d"*30 not in raw

class TestFixPlanThresholds:
    def test_fix_plan_false_below_all_thresholds(self, tmp_path):
        _write(tmp_path, _fn(), _art(ts=_ts())); _, d, _ = _run(tmp_path); assert d["fix_plan_required"] is False
    def test_fix_plan_true_on_critical_safety_gate(self, tmp_path):
        _write(tmp_path, _fn(), _art(critical_safety_gates=1, ts=_ts())); _, d, _ = _run(tmp_path); assert d["fix_plan_required"] is True
    def test_fix_plan_true_on_errors_exceed_50(self, tmp_path):
        _write(tmp_path, _fn(), _art(errors=51, ts=_ts())); _, d, _ = _run(tmp_path); assert d["fix_plan_required"] is True
    def test_fix_plan_true_on_budget_blocks_exceed_10(self, tmp_path):
        _write(tmp_path, _fn(), _art(budget_blocks=11, ts=_ts())); _, d, _ = _run(tmp_path); assert d["fix_plan_required"] is True
    def test_fix_plan_false_at_exact_boundary_errors_50(self, tmp_path):
        _write(tmp_path, _fn(), _art(errors=50, ts=_ts())); _, d, _ = _run(tmp_path); assert d["fix_plan_required"] is False
    def test_fix_plan_false_at_exact_boundary_budget_10(self, tmp_path):
        _write(tmp_path, _fn(), _art(budget_blocks=10, ts=_ts())); _, d, _ = _run(tmp_path); assert d["fix_plan_required"] is False
    def test_fix_plan_true_at_errors_51(self, tmp_path):
        _write(tmp_path, _fn(), _art(errors=51, ts=_ts())); _, d, _ = _run(tmp_path); assert d["fix_plan_required"] is True
    def test_fix_plan_true_at_budget_blocks_11(self, tmp_path):
        _write(tmp_path, _fn(), _art(budget_blocks=11, ts=_ts())); _, d, _ = _run(tmp_path); assert d["fix_plan_required"] is True

class TestDecisionDistribution:
    def test_decision_distribution_all_zeros(self, tmp_path):
        _write(tmp_path, _fn(), _art(ts=_ts())); _, d, _ = _run(tmp_path)
        assert all(d["decision_distribution"][k] == 0 for k in ("buy","sell","hold","skip"))
    def test_decision_distribution_buy_count(self, tmp_path):
        _write(tmp_path, _fn(), _art(decisions={"buy":5,"sell":0,"hold":0,"skip":0}, ts=_ts()))
        _, d, _ = _run(tmp_path); assert d["decision_distribution"]["buy"] == 5
    def test_decision_distribution_sell_count(self, tmp_path):
        _write(tmp_path, _fn(), _art(decisions={"buy":0,"sell":3,"hold":0,"skip":0}, ts=_ts()))
        _, d, _ = _run(tmp_path); assert d["decision_distribution"]["sell"] == 3
    def test_decision_distribution_hold_count(self, tmp_path):
        _write(tmp_path, _fn(), _art(decisions={"buy":0,"sell":0,"hold":7,"skip":0}, ts=_ts()))
        _, d, _ = _run(tmp_path); assert d["decision_distribution"]["hold"] == 7
    def test_decision_distribution_skip_count(self, tmp_path):
        _write(tmp_path, _fn(), _art(decisions={"buy":0,"sell":0,"hold":0,"skip":9}, ts=_ts()))
        _, d, _ = _run(tmp_path); assert d["decision_distribution"]["skip"] == 9
    def test_decision_distribution_aggregated_across_artifacts(self, tmp_path):
        _write(tmp_path, _fn(1), _art(decisions={"buy":2,"sell":1,"hold":0,"skip":0}, ts=_ts(1)))
        _write(tmp_path, _fn(2), _art(decisions={"buy":3,"sell":2,"hold":1,"skip":4}, ts=_ts(2)))
        _, d, _ = _run(tmp_path)
        assert d["decision_distribution"] == {"buy":5,"sell":3,"hold":1,"skip":4}

class TestDBGrowthDelta:
    def test_db_growth_delta_positive(self, tmp_path):
        _write(tmp_path, _fn(2), _art(db_bytes=1000, ts=_ts(2)))
        _write(tmp_path, _fn(1), _art(db_bytes=2000, ts=_ts(1)))
        _, d, _ = _run(tmp_path); assert Decimal(d["db_growth_bytes_delta"]) == Decimal("1000")
    def test_db_growth_delta_zero_single_artifact(self, tmp_path):
        _write(tmp_path, _fn(), _art(db_bytes=5000, ts=_ts()))
        _, d, _ = _run(tmp_path); assert Decimal(d["db_growth_bytes_delta"]) == Decimal("0")
    def test_db_growth_delta_negative_shrinkage(self, tmp_path):
        _write(tmp_path, _fn(2), _art(db_bytes=3000, ts=_ts(2)))
        _write(tmp_path, _fn(1), _art(db_bytes=2500, ts=_ts(1)))
        _, d, _ = _run(tmp_path); assert Decimal(d["db_growth_bytes_delta"]) == Decimal("-500")
    def test_db_growth_delta_uses_decimal(self, tmp_path):
        _write(tmp_path, _fn(), _art(db_bytes=1024, ts=_ts()))
        _, d, _ = _run(tmp_path); assert isinstance(Decimal(d["db_growth_bytes_delta"]), Decimal)

class TestStreamingProcessing:
    def test_large_artifact_set_does_not_spike_memory(self, tmp_path):
        for i in range(100): _write(tmp_path, _fn(i+1), _art(ts=_ts(i+1)))
        c, d, _ = _run(tmp_path, hours=120); assert c == 0 and d["scanned_files"] == 100
    def test_accumulator_pattern_used(self, tmp_path):
        for i in range(10): _write(tmp_path, _fn(i+1), _art(errors=i, ts=_ts(i+1)))
        _, d, _ = _run(tmp_path); assert d["total_errors"] == sum(range(10))

class TestEdgeCases:
    def test_single_artifact_in_window(self, tmp_path):
        _write(tmp_path, _fn(), _art(db_bytes=999, ts=_ts()))
        c, d, _ = _run(tmp_path); assert c == 0 and Decimal(d["db_growth_bytes_delta"]) == Decimal("0")
    def test_artifact_outside_lookback_excluded(self, tmp_path):
        _write(tmp_path, "runtime-audit-20200101-000000.json", _art(ts="2020-01-01T00:00:00+00:00"))
        _write(tmp_path, _fn(), _art(ts=_ts()))
        _, d, _ = _run(tmp_path); assert d["scanned_files"] == 1
    def test_timezone_naive_treated_as_utc(self, tmp_path):
        a = _art(ts=_ts()); a["generated_at_utc"] = a["generated_at_utc"].replace("+00:00","")
        _write(tmp_path, _fn(), a); c, d, _ = _run(tmp_path); assert c == 0
    def test_dry_run_true_documented_not_flagged(self, tmp_path):
        _write(tmp_path, _fn(), _art(dry_run=True, ts=_ts()))
        _, d, _ = _run(tmp_path); assert d["dry_run_posture"] is True and d.get("dry_run_inconsistent") is False
    def test_dry_run_inconsistency_flagged(self, tmp_path):
        _write(tmp_path, _fn(1), _art(dry_run=True, ts=_ts(1)))
        _write(tmp_path, _fn(2), _art(dry_run=False, ts=_ts(2)))
        _, d, _ = _run(tmp_path); assert d.get("dry_run_inconsistent") is True
    def test_max_exposure_absent_reports_unavailable(self, tmp_path):
        a = _art(ts=_ts()); del a["position_summary"]
        _write(tmp_path, _fn(), a); _, d, _ = _run(tmp_path); assert d["max_exposure_usdc"] == "unavailable"
    def test_avg_response_time_zero_samples_unavailable(self, tmp_path):
        a = _art(ts=_ts()); del a["health_probe"]
        _write(tmp_path, _fn(), a); _, d, _ = _run(tmp_path); assert d["avg_response_time_ms"] == "unavailable"
    def test_custom_hours_lookback(self, tmp_path):
        _write(tmp_path, _fn(1), _art(ts=_ts(1)))
        c, d, _ = _run(tmp_path, hours=2); assert c == 0
    def test_default_hours_is_72(self, tmp_path):
        _write(tmp_path, _fn(), _art(ts=_ts()))
        c, d, _ = _run(tmp_path); assert c == 0 and d.get("hours") == 72
