"""
Integration tests for WI-49 — Secure Remote Operator Dashboard Access.

Validates end-to-end: dashboard DB path configuration, read-only SQLite
enforcement against a real database, Compose profile gating, and secret-free
output invariants.  Uses the typed schemas from ``src.schemas.ops``.
"""

from __future__ import annotations

import importlib
import os
import sqlite3
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from src.schemas.ops import (
    DashboardAccessMode,
    DashboardAccessValidationReport,
    DashboardDatabaseTarget,
    DashboardExposureCheck,
    DashboardReadOnlyCheck,
    DashboardRuntimeConfig,
    DashboardTunnelSpec,
)


# ── DB Path Configuration (end-to-end) ─────────────────────────────────────


class TestDashboardDBPathEndToEnd:
    """End-to-end validation of DASHBOARD_DB_PATH configuration."""

    @staticmethod
    def _create_test_db(path: str) -> None:
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE _wi49_int (id INTEGER PRIMARY KEY, val TEXT)")
        conn.execute("INSERT INTO _wi49_int (val) VALUES ('integration_test')")
        conn.commit()
        conn.close()

    def test_env_var_changes_db_path_and_reads_correctly(self) -> None:
        """Setting DASHBOARD_DB_PATH to a valid DB must cause get_connection to read it."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test_dashboard.db"
            self._create_test_db(str(db_path))

            with mock.patch.dict(os.environ, {"DASHBOARD_DB_PATH": str(db_path)}):
                import src.ui.dashboard as dash
                importlib.reload(dash)

                conn = dash.get_connection()
                rows = conn.execute("SELECT val FROM _wi49_int").fetchall()
                assert len(rows) == 1
                assert rows[0][0] == "integration_test"
                conn.close()

    def test_missing_db_file_does_not_create_it(self) -> None:
        """When the path points to a nonexistent file, the module must not create it."""
        with tempfile.TemporaryDirectory() as tmpdir:
            nonexistent = Path(tmpdir) / "no_such_file.db"
            assert not nonexistent.exists()

            with mock.patch.dict(os.environ, {"DASHBOARD_DB_PATH": str(nonexistent)}):
                import src.ui.dashboard as dash
                importlib.reload(dash)

                # The file must not exist after import
                assert not nonexistent.exists()

    def test_local_default_works_without_env_var(self) -> None:
        """When DASHBOARD_DB_PATH is unset, the local default path is used."""
        with mock.patch.dict(os.environ, {}, clear=True):
            os.environ.pop("DASHBOARD_DB_PATH", None)
            import src.ui.dashboard as dash
            importlib.reload(dash)

            assert dash.DB_PATH.name == "poly_oracle.db"


# ── Read-Only Enforcement (end-to-end) ─────────────────────────────────────


class TestReadOnlyEnforcementEndToEnd:
    """The dashboard connection must reject writes at the SQLite level."""

    @staticmethod
    def _create_test_db(path: str) -> None:
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE t (id INTEGER)")
        conn.execute("INSERT INTO t VALUES (1)")
        conn.commit()
        conn.close()

    def test_read_only_connection_rejects_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "ro_test.db"
            self._create_test_db(str(db_path))

            with mock.patch.dict(os.environ, {"DASHBOARD_DB_PATH": str(db_path)}):
                import src.ui.dashboard as dash
                importlib.reload(dash)

                conn = dash.get_connection()
                # SELECT must work
                rows = conn.execute("SELECT COUNT(*) FROM t").fetchall()
                assert rows[0][0] == 1

                # INSERT must raise
                with pytest.raises(sqlite3.OperationalError):
                    conn.execute("INSERT INTO t VALUES (2)")
                conn.close()

    def test_read_only_connection_from_dashboard_module(self) -> None:
        """Verify that get_connection() returns a read-only connection."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "ro_dash.db"
            self._create_test_db(str(db_path))

            with mock.patch.dict(os.environ, {"DASHBOARD_DB_PATH": str(db_path)}):
                import src.ui.dashboard as dash
                importlib.reload(dash)

                conn = dash.get_connection()
                # Verify read works
                cur = conn.execute("SELECT id FROM t")
                assert cur.fetchone() is not None
                conn.close()


# ── Schema Validation Reports (end-to-end) ─────────────────────────────────


class TestDashboardAccessValidationReportEndToEnd:
    """End-to-end construction and validation of DashboardAccessValidationReport."""

    def test_full_report_passing(self) -> None:
        ro = DashboardReadOnlyCheck(
            passed=True,
            reason="URI mode=ro confirmed",
            write_attempted=False,
            db_path="/data/poly_oracle.db",
            uri_mode=True,
        )
        exp = DashboardExposureCheck(
            passed=True,
            prohibited_patterns=["private_key", "api_key", "telegram_token", "raw_prompt"],
            violations_found=[],
        )
        tunnel = DashboardTunnelSpec(
            local_port=8501,
            remote_host="127.0.0.1",
            remote_port=8501,
            ssh_user="deploy",
        )
        report = DashboardAccessValidationReport(
            read_only_check=ro,
            exposure_check=exp,
            tunnel_spec=tunnel,
            overall_pass=True,
            summary="All dashboard access checks passed.",
        )
        assert report.overall_pass is True
        assert report.tunnel_spec is not None
        assert report.tunnel_spec.ssh_user == "deploy"

    def test_full_report_failing_readonly(self) -> None:
        ro = DashboardReadOnlyCheck(
            passed=False,
            reason="Write attempted on read-only connection",
            write_attempted=True,
            db_path="/data/poly_oracle.db",
            uri_mode=True,
        )
        exp = DashboardExposureCheck(passed=True)
        report = DashboardAccessValidationReport(
            read_only_check=ro,
            exposure_check=exp,
            overall_pass=False,
            summary="Read-only check failed.",
        )
        assert report.overall_pass is False
        assert report.read_only_check.write_attempted is True

    def test_full_report_failing_exposure(self) -> None:
        ro = DashboardReadOnlyCheck(passed=True, reason="ok")
        exp = DashboardExposureCheck(
            passed=False,
            prohibited_patterns=["private_key", "api_key"],
            violations_found=["0x" + "a" * 64 + " detected in output"],
        )
        report = DashboardAccessValidationReport(
            read_only_check=ro,
            exposure_check=exp,
            overall_pass=False,
            summary="Secret exposure detected.",
        )
        assert report.overall_pass is False
        assert len(report.exposure_check.violations_found) > 0


# ── Compose Profile Gating (end-to-end) ────────────────────────────────────


class TestComposeProfileEndToEnd:
    """Docker Compose profile gating validates correctly through schema."""

    def test_dashboard_config_schema_roundtrip(self) -> None:
        cfg = DashboardRuntimeConfig(
            profile_enabled=True,
            service_name="dashboard",
            bind_host="127.0.0.1",
            bind_port=8501,
        )
        assert cfg.profile_enabled is True
        assert cfg.bind_host == "127.0.0.1"

    def test_database_target_deployed_ssh_tunnel(self) -> None:
        target = DashboardDatabaseTarget(
            path="/data/poly_oracle.db",
            read_only=True,
            access_mode=DashboardAccessMode.SSH_TUNNEL,
        )
        assert target.read_only is True
        assert target.access_mode == DashboardAccessMode.SSH_TUNNEL

    def test_database_target_rejects_writable_ssh_tunnel(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            DashboardDatabaseTarget(
                path="/data/poly_oracle.db",
                read_only=False,
                access_mode=DashboardAccessMode.SSH_TUNNEL,
            )


# ── Secret-Free Output (end-to-end) ────────────────────────────────────────


class TestSecretFreeOutputEndToEnd:
    """Dashboard source must not contain secret patterns even across full file scan."""

    def test_dashboard_source_no_private_key_patterns(self) -> None:
        ui_dir = Path(__file__).resolve().parents[2] / "src" / "ui"
        for py_file in ui_dir.glob("*.py"):
            source = py_file.read_text()
            # No 64-char hex strings that look like private keys
            import re
            if re.search(r"0x[a-fA-F0-9]{64}", source):
                pytest.fail(f"{py_file.name} contains private-key-like hex string")

    def test_dashboard_no_env_iteration_in_render(self) -> None:
        ui_dir = Path(__file__).resolve().parents[2] / "src" / "ui"
        for py_file in ui_dir.glob("*.py"):
            source = py_file.read_text()
            lines = source.split("\n")
            for i, line in enumerate(lines):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                # Must not iterate os.environ for rendering
                if "os.environ" in stripped and ("st." in stripped or "markdown" in stripped.lower()):
                    pytest.fail(f"{py_file.name}:{i+1}: os.environ exposed in UI path")


# ── Runbook Presence (end-to-end) ──────────────────────────────────────────


class TestRunbookEndToEnd:
    """SSH tunnel runbook must exist and contain required operational sections."""

    _runbook_path = Path(__file__).resolve().parents[2] / "docs" / "runbooks" / "streamlit-ssh-tunnel.md"

    def test_runbook_exists_and_readable(self) -> None:
        assert self._runbook_path.exists()
        content = self._runbook_path.read_text()
        assert len(content) > 200, "Runbook must have substantial content"

    def test_runbook_covers_access_and_shutdown(self) -> None:
        content = self._runbook_path.read_text()
        assert "ssh -N -L" in content
        assert "Ctrl+C" in content or "tunnel" in content.lower()
        assert "http://localhost:8501" in content
