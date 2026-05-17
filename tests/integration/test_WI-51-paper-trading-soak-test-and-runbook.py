"""Integration tests for WI-51: Paper-Trading Soak Test and Runbook.

Covers: schema existence, report generation, redaction, failed-readiness
reporting, missing-metrics handling, dry-run-required failure, output path
constraints, and all edge cases from business_logic_WI-51.
"""

from __future__ import annotations

import importlib
import json
import tempfile
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Import modules under test
# ---------------------------------------------------------------------------


def _import_soak_schemas():
    try:
        return importlib.import_module("src.schemas.soak")
    except ModuleNotFoundError:
        pytest.fail("src.schemas.soak module not found")


def _import_evidence_collector():
    try:
        spec = importlib.util.spec_from_file_location(
            "collect_soak_evidence",
            Path("scripts/ops/collect_soak_evidence.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except (FileNotFoundError, ModuleNotFoundError, AttributeError):
        pytest.fail("scripts/ops/collect_soak_evidence.py not found or not importable")


@pytest.fixture
def soak_schemas():
    return _import_soak_schemas()


@pytest.fixture
def collector():
    return _import_evidence_collector()


# ═══════════════════════════════════════════════════════════════════════════
# Schema existence tests
# ═══════════════════════════════════════════════════════════════════════════


def test_soak_verdict_schema_exists(soak_schemas):
    assert hasattr(soak_schemas, "SoakVerdict")
    assert soak_schemas.SoakVerdict.PASS == "pass"


def test_soak_probe_status_schema_exists(soak_schemas):
    assert hasattr(soak_schemas, "SoakProbeStatus")
    assert soak_schemas.SoakProbeStatus.PASS == "pass"


def test_soak_probe_result_schema_exists(soak_schemas):
    assert hasattr(soak_schemas, "SoakProbeResult")
    result = soak_schemas.SoakProbeResult(
        probe_name="test",
        status=soak_schemas.SoakProbeStatus.PASS,
    )
    assert result.probe_name == "test"


def test_soak_service_status_schema_exists(soak_schemas):
    assert hasattr(soak_schemas, "SoakServiceStatus")
    ss = soak_schemas.SoakServiceStatus(running=True, restart_count=0)
    assert ss.running is True


def test_soak_health_evidence_schema_exists(soak_schemas):
    assert hasattr(soak_schemas, "SoakHealthEvidence")
    probe = soak_schemas.SoakProbeResult(
        probe_name="health",
        status=soak_schemas.SoakProbeStatus.PASS,
    )
    he = soak_schemas.SoakHealthEvidence(health_probe=probe)
    assert he.healthz_reachable is False


def test_soak_metrics_evidence_schema_exists(soak_schemas):
    assert hasattr(soak_schemas, "SoakMetricsEvidence")
    probe = soak_schemas.SoakProbeResult(
        probe_name="metrics",
        status=soak_schemas.SoakProbeStatus.PASS,
    )
    me = soak_schemas.SoakMetricsEvidence(metrics_probe=probe)
    assert me.metrics_reachable is False


def test_soak_database_evidence_schema_exists(soak_schemas):
    assert hasattr(soak_schemas, "SoakDatabaseEvidence")
    probe = soak_schemas.SoakProbeResult(
        probe_name="database",
        status=soak_schemas.SoakProbeStatus.PASS,
    )
    de = soak_schemas.SoakDatabaseEvidence(db_probe=probe)
    assert de.db_file_exists is False


def test_soak_recovery_evidence_schema_exists(soak_schemas):
    assert hasattr(soak_schemas, "SoakRecoveryEvidence")
    probe = soak_schemas.SoakProbeResult(
        probe_name="recovery",
        status=soak_schemas.SoakProbeStatus.INCOMPLETE,
    )
    re_ev = soak_schemas.SoakRecoveryEvidence(recovery_probe=probe)
    assert re_ev.recovery_tested is False


def test_soak_evidence_report_schema_exists(soak_schemas):
    assert hasattr(soak_schemas, "SoakEvidenceReport")
    report = soak_schemas.SoakEvidenceReport()
    assert report.live_trading_authorized is False
    assert report.verdict == soak_schemas.SoakVerdict.INCOMPLETE


# ═══════════════════════════════════════════════════════════════════════════
# Evidence collector module existence
# ═══════════════════════════════════════════════════════════════════════════


def test_collect_soak_evidence_module_exists(collector):
    assert hasattr(collector, "main")
    assert hasattr(collector, "_probe_dry_run")
    assert hasattr(collector, "_probe_duration")
    assert hasattr(collector, "_probe_health")
    assert hasattr(collector, "_probe_metrics")
    assert hasattr(collector, "_probe_database")
    assert hasattr(collector, "_probe_compose_service")
    assert hasattr(collector, "_probe_telegram")
    assert hasattr(collector, "_probe_recovery")
    assert hasattr(collector, "_validate_report")


# ═══════════════════════════════════════════════════════════════════════════
# Report generation tests
# ═══════════════════════════════════════════════════════════════════════════


class TestReportGeneration:
    """Tests for markdown and JSON report writing."""

    def test_collect_soak_evidence_writes_markdown_report(self, collector):
        with tempfile.TemporaryDirectory() as tmpdir:
            report = {
                "report_id": "soak-test",
                "target_host": "localhost",
                "soak_start_utc": "2026-05-01T00:00:00+00:00",
                "soak_end_utc": "2026-05-02T00:00:00+00:00",
                "duration_hours": 24.0,
                "dry_run_confirmed": True,
                "verdict": "pass",
                "verdict_reason": "All gates passed",
                "live_trading_authorized": False,
                "exit_code": 0,
                "probes": [],
                "service_status": None,
                "health_evidence": None,
                "metrics_evidence": None,
                "database_evidence": None,
                "recovery_evidence": None,
                "telegram_enabled": False,
                "telegram_status": "not_applicable",
            }
            out = Path(tmpdir)
            collector._write_markdown(report, out / "test.md")
            content = (out / "test.md").read_text()
            assert "# Phase 14 Paper-Trading Soak Report" in content
            assert "**Verdict:** **PASS**" in content

    def test_collect_soak_evidence_writes_json_report(self, collector):
        with tempfile.TemporaryDirectory() as tmpdir:
            report = {
                "report_id": "soak-test",
                "target_host": "localhost",
                "duration_hours": 24.0,
                "dry_run_confirmed": True,
                "verdict": "pass",
                "verdict_reason": "All gates passed",
                "live_trading_authorized": False,
                "exit_code": 0,
                "probes": [],
            }
            out = Path(tmpdir)
            collector._write_json(report, out / "test.json")
            data = json.loads((out / "test.json").read_text())
            assert data["verdict"] == "pass"
            assert data["live_trading_authorized"] is False

    def test_collect_soak_evidence_creates_output_directory_if_missing(self, collector):
        """_write_report creates docs/operations/ if it does not exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(collector, "_PROJECT_ROOT", Path(tmpdir)):
                out = Path(tmpdir) / "docs" / "operations"
                assert not out.exists()
                collector._write_report(
                    {
                        "report_id": "x",
                        "target_host": "localhost",
                        "verdict": "pass",
                        "duration_hours": 24.0,
                        "probes": [],
                        "live_trading_authorized": False,
                        "exit_code": 0,
                        "verdict_reason": "test",
                    }
                )
                assert out.exists()
                assert (out / "phase14-soak-report.md").exists()
                assert (out / "phase14-soak-report.json").exists()


# ═══════════════════════════════════════════════════════════════════════════
# Dry-run gate tests (.env + runtime /readyz)
# ═══════════════════════════════════════════════════════════════════════════


class TestDryRunGate:
    """Tests for DRY_RUN=true enforcement (static .env + runtime /readyz)."""

    def test_collect_soak_evidence_exits_nonzero_when_dry_run_false(self, collector):
        """DRY_RUN=false in .env → fail."""
        with patch.object(Path, "exists", return_value=True):
            with patch.object(Path, "read_text", return_value="DRY_RUN=false\n"):
                with patch.object(
                    collector, "_http_get", side_effect=urllib.error.URLError("no")
                ):
                    ok, probe = collector._probe_dry_run("127.0.0.1")
                    assert ok is False
                    assert probe["status"] == "fail"

    def test_collect_soak_evidence_exits_nonzero_when_dry_run_missing(self, collector):
        """DRY_RUN key missing from .env → fail."""
        with patch.object(Path, "exists", return_value=True):
            with patch.object(Path, "read_text", return_value="OTHER_KEY=value\n"):
                with patch.object(
                    collector, "_http_get", side_effect=urllib.error.URLError("no")
                ):
                    ok, probe = collector._probe_dry_run("127.0.0.1")
                    assert ok is False
                    assert probe["status"] == "fail"
                    assert probe["failure_reason"] == "dry_run_missing"

    def test_collect_soak_evidence_does_not_emit_passing_report_when_dry_run_false(
        self, collector
    ):
        """When dry_run is false, _compute_verdict returns fail."""
        report = {
            "probes": [
                {
                    "probe_name": "dry_run_guard",
                    "status": "fail",
                    "detail": "DRY_RUN is not true",
                    "failure_reason": "dry_run_false",
                },
            ],
        }
        verdict, reason = collector._compute_verdict(report)
        assert verdict == "fail"
        assert "DRY_RUN" in reason

    def test_dry_run_checks_runtime_readyz(self, collector):
        """Runtime /readyz dry_run=true bolsters .env confirmation."""
        with patch.object(Path, "exists", return_value=True):
            with patch.object(Path, "read_text", return_value="DRY_RUN=true\n"):
                with patch.object(collector, "_http_get") as mock_get:
                    mock_get.return_value = (
                        200,
                        '{"status":"READY","dry_run":true}',
                        {},
                    )
                    ok, probe = collector._probe_dry_run("127.0.0.1")
                    assert ok is True
                    assert "runtime /readyz" in probe["detail"]

    def test_dry_run_fails_when_runtime_readyz_says_false(self, collector):
        """Runtime /readyz dry_run=false fails EVEN IF .env says true."""
        with patch.object(Path, "exists", return_value=True):
            with patch.object(Path, "read_text", return_value="DRY_RUN=true\n"):
                with patch.object(collector, "_http_get") as mock_get:
                    mock_get.return_value = (
                        200,
                        '{"status":"READY","dry_run":false}',
                        {},
                    )
                    ok, probe = collector._probe_dry_run("127.0.0.1")
                    assert ok is False
                    assert probe["status"] == "fail"
                    assert probe["failure_reason"] == "dry_run_false"
                    assert "Runtime /readyz reports dry_run=false" in probe["detail"]

    def test_dry_run_fails_when_runtime_readyz_missing_dry_run(self, collector):
        """Runtime /readyz must explicitly confirm dry_run=true."""
        with patch.object(Path, "exists", return_value=True):
            with patch.object(Path, "read_text", return_value="DRY_RUN=true\n"):
                with patch.object(collector, "_http_get") as mock_get:
                    mock_get.return_value = (200, '{"status":"READY"}', {})
                    ok, probe = collector._probe_dry_run("127.0.0.1")
                    assert ok is False
                    assert probe["status"] == "fail"
                    assert probe["failure_reason"] == "runtime_dry_run_unconfirmed"


# ═══════════════════════════════════════════════════════════════════════════
# Soak duration gate tests
# ═══════════════════════════════════════════════════════════════════════════


class TestSoakDuration:
    """Tests for minimum soak duration enforcement."""

    def test_collect_soak_evidence_fails_when_soak_shorter_than_24_hours(
        self, collector
    ):
        one_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        duration_hours, probe = collector._probe_duration(one_hour_ago)
        assert duration_hours < 24.0
        assert probe["status"] == "fail"
        assert probe["failure_reason"] == "duration_too_short"

    def test_collect_soak_evidence_passes_when_soak_at_least_24_hours(self, collector):
        twenty_five_hours_ago = datetime.now(timezone.utc) - timedelta(hours=25)
        duration_hours, probe = collector._probe_duration(twenty_five_hours_ago)
        assert duration_hours >= 24.0
        assert probe["status"] == "pass"


# ═══════════════════════════════════════════════════════════════════════════
# Health & readiness evidence tests
# ═══════════════════════════════════════════════════════════════════════════


class TestHealthEvidence:
    """Tests for health/readiness endpoint probing."""

    def test_soak_evidence_includes_health_endpoint_probe(self, collector):
        with patch.object(collector, "_http_get") as mock_get:
            mock_get.side_effect = [
                (200, "OK", {"content-type": "text/plain"}),
                (200, '{"status":"READY"}', {"content-type": "application/json"}),
            ]
            evidence = collector._probe_health("127.0.0.1")
            assert evidence["healthz_reachable"] is True
            assert evidence["healthz_status_code"] == 200

    def test_soak_evidence_includes_readiness_endpoint_probe(self, collector):
        with patch.object(collector, "_http_get") as mock_get:
            mock_get.side_effect = [
                (200, "OK", {}),
                (200, '{"status":"READY"}', {}),
            ]
            evidence = collector._probe_health("127.0.0.1")
            assert evidence["readyz_reachable"] is True
            assert evidence["readyz_status"] == "READY"

    def test_soak_evidence_captures_degraded_readiness_reason(self, collector):
        with patch.object(collector, "_http_get") as mock_get:
            mock_get.side_effect = [
                (200, "OK", {}),
                (
                    200,
                    '{"status":"DEGRADED","checks":{"database":"ok","websocket":"stale"}}',
                    {},
                ),
            ]
            evidence = collector._probe_health("127.0.0.1")
            assert evidence["readyz_status"] == "DEGRADED"
            assert evidence["health_probe"]["status"] == "fail"
            assert evidence["health_probe"]["failure_reason"] == "readyz_degraded"
            assert "websocket" in evidence["degraded_reason"]

    def test_soak_evidence_fails_on_uppercase_not_ready(self, collector):
        with patch.object(collector, "_http_get") as mock_get:
            mock_get.side_effect = [
                (200, "OK", {}),
                (503, '{"status":"NOT_READY","checks":{"database":"unreachable"}}', {}),
            ]
            evidence = collector._probe_health("127.0.0.1")
            assert evidence["readyz_status"] == "NOT_READY"
            assert evidence["health_probe"]["status"] == "fail"
            assert evidence["health_probe"]["failure_reason"] == "readyz_not_ready"

    def test_soak_evidence_fails_when_health_endpoint_unreachable(self, collector):
        with patch.object(collector, "_http_get") as mock_get:
            mock_get.side_effect = urllib.error.URLError("connection refused")
            evidence = collector._probe_health("127.0.0.1")
            assert evidence["healthz_reachable"] is False
            assert evidence["health_probe"]["status"] == "fail"
            assert evidence["health_probe"]["failure_reason"] == "healthz_unreachable"


# ═══════════════════════════════════════════════════════════════════════════
# Metrics evidence tests
# ═══════════════════════════════════════════════════════════════════════════


class TestMetricsEvidence:
    """Tests for /metrics endpoint probing."""

    def test_soak_evidence_includes_metrics_endpoint_probe(self, collector):
        prom_text = "# HELP test Test metric\ntest 1.0\n"
        with patch.object(collector, "_http_get") as mock_get:
            mock_get.return_value = (200, prom_text, {"content-type": "text/plain"})
            evidence = collector._probe_metrics("127.0.0.1")
            assert evidence["metrics_reachable"] is True
            assert evidence["prometheus_format_valid"] is True
            assert evidence["metrics_probe"]["status"] == "pass"

    def test_soak_evidence_fails_when_metrics_endpoint_missing(self, collector):
        with patch.object(collector, "_http_get") as mock_get:
            mock_get.side_effect = urllib.error.URLError("connection refused")
            evidence = collector._probe_metrics("127.0.0.1")
            assert evidence["metrics_reachable"] is False
            assert evidence["metrics_probe"]["status"] == "fail"
            assert evidence["metrics_probe"]["failure_reason"] == "metrics_unreachable"


# ═══════════════════════════════════════════════════════════════════════════
# Database evidence tests
# ═══════════════════════════════════════════════════════════════════════════


class TestDatabaseEvidence:
    """Tests for SQLite persistence evidence with growth delta."""

    def _make_mock_connection(self, decision_count=5, snapshot_count=10):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchone.side_effect = [
            {"cnt": decision_count},
            {"cnt": snapshot_count},
        ]
        return mock_conn

    def test_soak_evidence_includes_sqlite_file_presence(self, collector):
        soak_start = datetime.now(timezone.utc) - timedelta(hours=25)
        with patch.object(Path, "exists", return_value=True):
            with patch.object(Path, "is_file", return_value=True):
                with patch.object(Path, "stat") as mock_stat:
                    mock_stat.return_value.st_size = 4096
                    with patch("sqlite3.connect") as mock_connect:
                        mock_connect.return_value = self._make_mock_connection(5, 10)
                        evidence = collector._probe_database(
                            "/fake/db.db", soak_start, baseline_size=0
                        )
                        assert evidence["db_file_exists"] is True
                        assert evidence["db_file_size_bytes"] == 4096

    def test_soak_evidence_includes_sqlite_file_growth(self, collector):
        """Growth = current_size - baseline_size."""
        soak_start = datetime.now(timezone.utc) - timedelta(hours=25)
        with patch.object(Path, "exists", return_value=True):
            with patch.object(Path, "is_file", return_value=True):
                with patch.object(Path, "stat") as mock_stat:
                    mock_stat.return_value.st_size = 8192
                    with patch("sqlite3.connect") as mock_connect:
                        mock_connect.return_value = self._make_mock_connection(5, 10)
                        evidence = collector._probe_database(
                            "/fake/db.db",
                            soak_start,
                            baseline_size=4096,
                        )
                        assert evidence["db_growth_bytes"] == 4096
                        assert evidence["db_grew"] is True

    def test_soak_evidence_database_no_growth_with_baseline(self, collector):
        """When current size equals baseline, no growth detected."""
        soak_start = datetime.now(timezone.utc) - timedelta(hours=25)
        with patch.object(Path, "exists", return_value=True):
            with patch.object(Path, "is_file", return_value=True):
                with patch.object(Path, "stat") as mock_stat:
                    mock_stat.return_value.st_size = 4096
                    with patch("sqlite3.connect") as mock_connect:
                        mock_connect.return_value = self._make_mock_connection(0, 0)
                        evidence = collector._probe_database(
                            "/fake/db.db",
                            soak_start,
                            baseline_size=4096,
                        )
                        assert evidence["db_growth_bytes"] == 0
                        assert evidence["db_grew"] is False
                        assert evidence["db_probe"]["status"] == "fail"
                        assert (
                            evidence["db_probe"]["failure_reason"]
                            == "no_persistence_activity"
                        )

    def test_soak_evidence_flags_missing_persistence_activity(self, collector):
        """DB exists but has 0 decisions and 0 snapshots → incomplete."""
        soak_start = datetime.now(timezone.utc) - timedelta(hours=25)
        with patch.object(Path, "exists", return_value=True):
            with patch.object(Path, "is_file", return_value=True):
                with patch.object(Path, "stat") as mock_stat:
                    mock_stat.return_value.st_size = 4096
                    with patch("sqlite3.connect") as mock_connect:
                        mock_connect.return_value = self._make_mock_connection(0, 0)
                        evidence = collector._probe_database(
                            "/fake/db.db", soak_start, baseline_size=0
                        )
                        assert evidence["db_probe"]["status"] == "incomplete"

    def test_soak_evidence_handles_locked_sqlite(self, collector):
        soak_start = datetime.now(timezone.utc) - timedelta(hours=25)
        with patch.object(Path, "exists", return_value=True):
            with patch.object(Path, "is_file", return_value=True):
                with patch.object(Path, "stat") as mock_stat:
                    mock_stat.return_value.st_size = 4096
                    with patch("sqlite3.connect") as mock_connect:
                        import sqlite3

                        mock_connect.side_effect = sqlite3.OperationalError(
                            "database is locked"
                        )
                        evidence = collector._probe_database(
                            "/fake/db.db", soak_start, baseline_size=0
                        )
                        assert evidence["sqlite_locked"] is True
                        assert evidence["db_probe"]["status"] == "fail"
                        assert evidence["db_probe"]["failure_reason"] == "sqlite_locked"

    def test_soak_evidence_includes_decision_count(self, collector):
        soak_start = datetime.now(timezone.utc) - timedelta(hours=25)
        with patch.object(Path, "exists", return_value=True):
            with patch.object(Path, "is_file", return_value=True):
                with patch.object(Path, "stat") as mock_stat:
                    mock_stat.return_value.st_size = 4096
                    with patch("sqlite3.connect") as mock_connect:
                        mock_connect.return_value = self._make_mock_connection(42, 100)
                        evidence = collector._probe_database(
                            "/fake/db.db", soak_start, baseline_size=0
                        )
                        assert evidence["recent_decision_count"] == 42
                        assert evidence["recent_snapshot_count"] == 100
                        assert evidence["db_probe"]["status"] == "pass"

    def test_soak_evidence_includes_market_snapshot_count(self, collector):
        soak_start = datetime.now(timezone.utc) - timedelta(hours=25)
        with patch.object(Path, "exists", return_value=True):
            with patch.object(Path, "is_file", return_value=True):
                with patch.object(Path, "stat") as mock_stat:
                    mock_stat.return_value.st_size = 4096
                    with patch("sqlite3.connect") as mock_connect:
                        mock_connect.return_value = self._make_mock_connection(3, 77)
                        evidence = collector._probe_database(
                            "/fake/db.db", soak_start, baseline_size=0
                        )
                        assert evidence["recent_snapshot_count"] == 77

    def test_soak_evidence_counts_sqlalchemy_sqlite_datetime_strings(self, collector):
        """SQLite DateTime values with a space separator count as recent."""
        import sqlite3

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "soak.db"
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE agent_decision_logs (evaluated_at TEXT)")
            conn.execute("CREATE TABLE market_snapshots (captured_at TEXT)")
            conn.execute(
                "INSERT INTO agent_decision_logs VALUES (?)",
                ("2026-05-07 12:00:00.000000",),
            )
            conn.execute(
                "INSERT INTO market_snapshots VALUES (?)",
                ("2026-05-07 12:05:00.000000",),
            )
            conn.commit()
            conn.close()

            evidence = collector._probe_database(
                str(db_path),
                datetime(2026, 5, 7, 0, 0, tzinfo=timezone.utc),
                baseline_size=0,
            )

            assert evidence["recent_decision_count"] == 1
            assert evidence["recent_snapshot_count"] == 1
            assert evidence["db_probe"]["status"] == "pass"


# ═══════════════════════════════════════════════════════════════════════════
# Service status evidence tests
# ═══════════════════════════════════════════════════════════════════════════


class TestComposeService:
    """Tests for Compose service status probing."""

    def test_soak_evidence_includes_compose_service_status(self, collector):
        svc_json = json.dumps(
            [
                {
                    "State": "running",
                    "Status": "Up 2 hours",
                    "Name": "poly-oracle-agent-orchestrator-1",
                }
            ]
        )
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout=svc_json, stderr=""),
                MagicMock(returncode=0, stdout="0\n", stderr=""),
            ]
            svc_info, probe = collector._probe_compose_service()
            assert svc_info is not None
            assert svc_info["running"] is True
            assert svc_info["service_name"] == "orchestrator"

    def test_soak_evidence_includes_restart_count(self, collector):
        svc_json = json.dumps(
            [
                {
                    "State": "running",
                    "Status": "Up 2 hours",
                    "Name": "poly-oracle-agent-orchestrator-1",
                }
            ]
        )
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout=svc_json, stderr=""),
                MagicMock(returncode=0, stdout="2\n", stderr=""),
            ]
            svc_info, probe = collector._probe_compose_service()
            assert svc_info["restart_count"] == 2
            assert svc_info["restart_count_source"] == "docker_inspect"

    def test_soak_evidence_includes_restart_evidence_when_nonzero(self, collector):
        svc_json = json.dumps(
            [{"State": "restarting", "Status": "Restarting (3) 10 seconds ago"}]
        )
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=svc_json,
                stderr="",
            )
            svc_info, probe = collector._probe_compose_service()
            assert svc_info["running"] is False
            assert svc_info["restart_count"] == 3
            assert probe["status"] == "fail"


# ═══════════════════════════════════════════════════════════════════════════
# Telegram evidence tests
# ═══════════════════════════════════════════════════════════════════════════


class TestTelegramEvidence:
    """Tests for Telegram status recording."""

    def test_soak_evidence_records_telegram_not_applicable_when_disabled(
        self, collector
    ):
        evidence = collector._probe_telegram(telegram_enabled=False)
        assert evidence["status"] == "not_applicable"
        assert evidence["enabled"] is False
        assert evidence["telegram_probe"]["status"] == "not_applicable"


# ═══════════════════════════════════════════════════════════════════════════
# Recovery evidence tests
# ═══════════════════════════════════════════════════════════════════════════


class TestRecoveryEvidence:
    """Tests for restart/reboot recovery evidence."""

    def test_soak_evidence_marks_recovery_incomplete_when_not_tested(self, collector):
        evidence = collector._probe_recovery(
            recovery_tested=False, recovery_method=None
        )
        assert evidence["recovery_tested"] is False
        assert evidence["recovery_probe"]["status"] == "incomplete"

    def test_soak_evidence_marks_recovery_pass_when_tested(self, collector):
        """When recovery was tested with valid method, probe status is PASS."""
        evidence = collector._probe_recovery(
            recovery_tested=True,
            recovery_method="docker compose restart",
            health_evidence={"health_probe": {"status": "pass"}},
            service_info={"running": True},
            db_evidence={"db_file_exists": True},
        )
        assert evidence["recovery_tested"] is True
        assert evidence["service_recovered"] is True
        assert evidence["recovery_probe"]["status"] == "pass"

    def test_recovery_fails_without_post_recovery_health(self, collector):
        evidence = collector._probe_recovery(
            recovery_tested=True,
            recovery_method="docker compose restart",
            health_evidence={"health_probe": {"status": "fail"}},
            service_info={"running": True},
            db_evidence={"db_file_exists": True},
        )
        assert evidence["service_recovered"] is False
        assert evidence["recovery_probe"]["status"] == "fail"
        assert evidence["recovery_probe"]["failure_reason"] == "recovery_not_verified"

    def test_recovery_fails_on_unknown_method(self, collector):
        """Unknown recovery method → FAIL."""
        evidence = collector._probe_recovery(
            recovery_tested=True,
            recovery_method="magic restart",
        )
        assert evidence["recovery_probe"]["status"] == "fail"
        assert evidence["recovery_probe"]["failure_reason"] == "invalid_recovery_method"


# ═══════════════════════════════════════════════════════════════════════════
# Redaction tests
# ═══════════════════════════════════════════════════════════════════════════


class TestRedaction:
    """Tests for secret redaction in reports."""

    def test_soak_report_redacts_api_keys(self, collector):
        text = "api_key=sk-ant-api03-abc123secret"
        result = collector._redact_text(text)
        assert "sk-ant-api03" not in result
        assert "[REDACTED:api_key]" in result

    def test_soak_report_redacts_wallet_private_keys(self, collector):
        text = "private_key=0xabcd1234abcd1234abcd1234abcd1234abcd1234abcd1234abcd1234abcd1234"
        result = collector._redact_text(text)
        assert "0xabcd" not in result
        assert "[REDACTED:private_key]" in result

    def test_soak_report_redacts_telegram_tokens(self, collector):
        text = "1234567890:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
        result = collector._redact_text(text)
        assert "1234567890:ABCDEF" not in result
        assert "[REDACTED:telegram_token]" in result

    def test_soak_report_redacts_raw_prompt_text(self, collector):
        text = "prompt_text=You are a trading agent analyzing markets..."
        result = collector._redact_text(text)
        assert "trading agent" not in result
        assert "[REDACTED:prompt_block]" in result

    def test_soak_report_redacts_reasoning_text(self, collector):
        text = "reasoning=The market shows strong momentum..."
        result = collector._redact_text(text)
        assert "strong momentum" not in result
        assert "[REDACTED:prompt_block]" in result

    def test_soak_report_redacts_token_ids(self, collector):
        text = "token_id=12345678901234"
        result = collector._redact_text(text)
        assert "12345678901234" not in result
        assert "[REDACTED:token_id]" in result

    def test_soak_report_redacts_condition_ids(self, collector):
        text = "condition=0xabcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
        result = collector._redact_text(text)
        assert "0xabcdef" not in result
        assert "[REDACTED:condition_id]" in result

    def test_soak_report_redacts_rpc_urls_with_embedded_credentials(self, collector):
        text = "https://user:password@rpc.example.com/path"
        result = collector._redact_text(text)
        assert "user:password" not in result
        assert "[REDACTED:rpc_url_credentials]" in result

    def test_soak_report_does_not_dump_full_env(self, collector):
        """_redact_dict redacts values for keys matching secret field names."""
        data = {
            "api_key": "secret123",
            "normal_field": "safe_value",
            "nested": {"token": "secret_nested"},
        }
        result = collector._redact_dict(data)
        assert result["api_key"] == "[REDACTED]"
        assert result["normal_field"] == "safe_value"
        assert result["nested"]["token"] == "[REDACTED]"

    def test_redaction_handles_strings_inside_lists(self, collector):
        """Secret-like strings inside lists are redacted."""
        data = {
            "messages": [
                "api_key=sk-secret-123",
                "normal message",
                {"nested_key": "value"},
            ]
        }
        result = collector._redact_dict(data)
        assert "[REDACTED:api_key]" in result["messages"][0]
        assert result["messages"][1] == "normal message"
        assert result["messages"][2]["nested_key"] == "value"


# ═══════════════════════════════════════════════════════════════════════════
# Report validation tests
# ═══════════════════════════════════════════════════════════════════════════


class TestReportValidation:
    """Tests for _validate_report schema validation."""

    def _valid_report(self):
        return {
            "report_id": "soak-test",
            "target_host": "localhost",
            "duration_hours": 24.0,
            "dry_run_confirmed": True,
            "verdict": "pass",
            "verdict_reason": "all good",
            "probes": [
                {"probe_name": "health", "status": "pass", "detail": "ok"},
            ],
            "live_trading_authorized": False,
            "exit_code": 0,
        }

    def test_validate_report_passes_for_valid_dict(self, collector):
        report = self._valid_report()
        result = collector._validate_report(report)
        assert result == report

    def test_validate_report_rejects_live_trading_authorized_true(self, collector):
        report = self._valid_report()
        report["live_trading_authorized"] = True
        with pytest.raises(ValueError, match="live_trading_authorized"):
            collector._validate_report(report)

    def test_validate_report_rejects_missing_required_fields(self, collector):
        report = {"verdict": "pass"}
        with pytest.raises(ValueError, match="missing required fields"):
            collector._validate_report(report)

    def test_validate_report_rejects_invalid_verdict(self, collector):
        report = self._valid_report()
        report["verdict"] = "maybe"
        with pytest.raises(ValueError, match="SoakEvidenceReport validation failed"):
            collector._validate_report(report)

    def test_validate_report_rejects_invalid_probe_status(self, collector):
        report = self._valid_report()
        report["probes"] = [{"probe_name": "x", "status": "invalid_status"}]
        with pytest.raises(ValueError, match="SoakEvidenceReport validation failed"):
            collector._validate_report(report)

    def test_validate_report_rejects_unbounded_failure_reason(self, collector):
        report = self._valid_report()
        report["probes"] = [
            {"probe_name": "x", "status": "fail", "failure_reason": "free_text"}
        ]
        with pytest.raises(ValueError, match="SoakEvidenceReport validation failed"):
            collector._validate_report(report)


# ═══════════════════════════════════════════════════════════════════════════
# Missing evidence → failed/incomplete verdict
# ═══════════════════════════════════════════════════════════════════════════


class TestVerdictComputation:
    """Tests for verdict logic."""

    def _mandatory_probes_pass(self):
        return [
            {"probe_name": "dry_run_guard", "status": "pass"},
            {"probe_name": "soak_duration", "status": "pass"},
            {"probe_name": "health", "status": "pass"},
            {"probe_name": "metrics", "status": "pass"},
            {"probe_name": "database", "status": "pass"},
        ]

    def test_soak_verdict_is_failed_when_mandatory_evidence_missing(self, collector):
        probes = self._mandatory_probes_pass()
        probes[2] = {
            "probe_name": "health",
            "status": "fail",
            "detail": "Health endpoint unreachable",
        }
        report = {"probes": probes}
        verdict, _ = collector._compute_verdict(report)
        assert verdict == "fail"

    def test_soak_verdict_fails_when_mandatory_probe_absent(self, collector):
        """Missing a mandatory probe entirely → fail."""
        probes = [
            {"probe_name": "dry_run_guard", "status": "pass"},
            {"probe_name": "soak_duration", "status": "pass"},
            # health probe MISSING
            {"probe_name": "metrics", "status": "pass"},
            {"probe_name": "database", "status": "pass"},
        ]
        report = {"probes": probes}
        verdict, reason = collector._compute_verdict(report)
        assert verdict == "fail"
        assert "health" in reason

    def test_soak_verdict_passes_when_all_gates_satisfied(self, collector):
        probes = self._mandatory_probes_pass() + [
            {"probe_name": "recovery", "status": "pass"},
            {"probe_name": "compose_service", "status": "pass"},
        ]
        report = {"probes": probes}
        verdict, _ = collector._compute_verdict(report)
        assert verdict == "pass"

    def test_soak_verdict_is_incomplete_when_recovery_not_tested(self, collector):
        probes = self._mandatory_probes_pass() + [
            {"probe_name": "recovery", "status": "incomplete"},
            {"probe_name": "compose_service", "status": "pass"},
        ]
        report = {"probes": probes}
        verdict, reason = collector._compute_verdict(report)
        assert verdict == "incomplete"
        assert "Recovery" in reason

    def test_soak_verdict_is_failed_when_any_mandatory_gate_fails(self, collector):
        probes = self._mandatory_probes_pass()
        probes[3] = {
            "probe_name": "metrics",
            "status": "fail",
            "detail": "Metrics endpoint unreachable",
        }
        report = {"probes": probes}
        verdict, _ = collector._compute_verdict(report)
        assert verdict == "fail"

    def test_recovery_pass_produces_pass_when_other_gates_ok(self, collector):
        probes = self._mandatory_probes_pass() + [
            {"probe_name": "recovery", "status": "pass"},
            {"probe_name": "compose_service", "status": "pass"},
        ]
        report = {"probes": probes}
        verdict, reason = collector._compute_verdict(report)
        assert verdict == "pass"
        assert "All mandatory" in reason


# ═══════════════════════════════════════════════════════════════════════════
# Runbook document tests
# ═══════════════════════════════════════════════════════════════════════════


class TestRunbook:
    """Tests for the paper-trading soak test runbook."""

    _RUNBOOK_PATH = Path("docs/runbooks/paper-trading-soak-test.md")

    @pytest.fixture(autouse=True)
    def _read_runbook(self):
        if not self._RUNBOOK_PATH.exists():
            pytest.skip("Runbook file not found")
        self.content = self._RUNBOOK_PATH.read_text()

    def test_paper_trading_soak_test_runbook_exists(self):
        assert self._RUNBOOK_PATH.exists()
        assert len(self.content) > 100

    def test_runbook_includes_setup_instructions(self):
        assert "Setup" in self.content or "setup" in self.content

    def test_runbook_includes_duration_requirements(self):
        assert "24 hours" in self.content or "24-hour" in self.content

    def test_runbook_includes_pass_fail_criteria(self):
        assert "Pass" in self.content and "Fail" in self.content

    def test_runbook_includes_recovery_steps(self):
        assert "Recovery" in self.content or "recovery" in self.content

    def test_runbook_states_dry_run_required(self):
        assert "DRY_RUN=true" in self.content

    def test_runbook_states_soak_does_not_authorize_live(self):
        assert (
            "does NOT authorize" in self.content or "does not authorize" in self.content
        )

    def test_runbook_records_db_baseline_at_soak_start(self):
        assert "BEFORE starting the soak" in self.content
        assert "/data/phase14_soak_db_baseline_size.txt" in self.content


# ═══════════════════════════════════════════════════════════════════════════
# Output path constraints
# ═══════════════════════════════════════════════════════════════════════════


class TestOutputPathConstraints:
    """Tests for output path behavior."""

    def test_collect_soak_evidence_only_writes_to_docs_operations(self, collector):
        """_write_report writes to project-root docs/operations/."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            ops_dir = project_root / "docs" / "operations"
            with patch.object(collector, "_PROJECT_ROOT", project_root):
                report = {
                    "report_id": "x",
                    "target_host": "localhost",
                    "verdict": "pass",
                    "duration_hours": 24.0,
                    "verdict_reason": "test",
                    "probes": [],
                    "live_trading_authorized": False,
                    "exit_code": 0,
                }
                collector._write_report(report)
                assert ops_dir.exists()
                assert (ops_dir / "phase14-soak-report.md").exists()
                assert (ops_dir / "phase14-soak-report.json").exists()

    def test_collect_soak_evidence_rejects_output_outside_project(self, collector):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(collector, "_PROJECT_ROOT", Path(tmpdir)):
                with patch.object(collector, "_OUTPUT_DIR", Path("../outside")):
                    with pytest.raises(ValueError, match="docs/operations"):
                        collector._write_report(
                            {
                                "report_id": "x",
                                "target_host": "localhost",
                                "verdict": "pass",
                                "duration_hours": 24.0,
                                "verdict_reason": "test",
                                "probes": [],
                                "live_trading_authorized": False,
                                "exit_code": 0,
                            }
                        )

    def test_collect_soak_evidence_report_makes_target_host_explicit(self, collector):
        """Report includes the target_host field."""
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir)
            report = {
                "report_id": "x",
                "target_host": "my-droplet",
                "verdict": "pass",
                "duration_hours": 24.0,
                "verdict_reason": "test",
                "probes": [],
                "live_trading_authorized": False,
                "exit_code": 0,
            }
            collector._write_json(report, out / "test.json")
            data = json.loads((out / "test.json").read_text())
            assert "target_host" in data
            assert data["target_host"] == "my-droplet"
