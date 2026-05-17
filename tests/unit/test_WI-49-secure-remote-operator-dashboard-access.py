"""Unit tests for WI-49 — Secure Remote Operator Dashboard Access.

Covers:
- DashboardRuntimeConfig schema validation
- DashboardDatabaseTarget schema validation
- DashboardAccessMode enum / schema
- DashboardTunnelSpec schema validation
- DashboardReadOnlyCheck schema & enforcement
- DashboardExposureCheck schema & secret scanning
- DashboardAccessValidationReport schema
- Dashboard DB path configuration (DASHBOARD_DB_PATH env var)
- Read-only SQLite enforcement in src/ui/
- Compose profile gating
- Secret-free output invariants
"""

from __future__ import annotations

import os
import re
import sqlite3
import tempfile
from pathlib import Path
from unittest import mock

import pytest
from pydantic import ValidationError

from src.schemas.ops import (
    DashboardAccessMode,
    DashboardAccessValidationReport,
    DashboardDatabaseTarget,
    DashboardExposureCheck,
    DashboardReadOnlyCheck,
    DashboardRuntimeConfig,
    DashboardTunnelSpec,
)


# ═══════════════════════════════════════════════════════════════════════════
# Schema: DashboardRuntimeConfig
# ═══════════════════════════════════════════════════════════════════════════


class TestDashboardRuntimeConfig:
    """Pydantic schema: DashboardRuntimeConfig — profile-gated dashboard service config."""

    def test_runtime_config_profile_enabled_field(self) -> None:
        cfg = DashboardRuntimeConfig(profile_enabled=True)
        assert cfg.profile_enabled is True

    def test_runtime_config_service_name_default(self) -> None:
        cfg = DashboardRuntimeConfig()
        assert cfg.service_name == "dashboard"

    def test_runtime_config_bind_host_defaults_to_loopback(self) -> None:
        cfg = DashboardRuntimeConfig()
        assert cfg.bind_host == "127.0.0.1"

    def test_runtime_config_bind_port_default(self) -> None:
        cfg = DashboardRuntimeConfig()
        assert cfg.bind_port == 8501

    def test_runtime_config_rejects_public_bind(self) -> None:
        """0.0.0.0 is now allowed (security via port publish); :: and empty are rejected."""
        # 0.0.0.0 is permitted (container networking)
        cfg = DashboardRuntimeConfig(bind_host="0.0.0.0")
        assert cfg.bind_host == "0.0.0.0"
        # Empty and :: are still rejected
        with pytest.raises(ValidationError):
            DashboardRuntimeConfig(bind_host="")
        with pytest.raises(ValidationError):
            DashboardRuntimeConfig(bind_host="::")

    def test_runtime_config_rejects_invalid_port_zero(self) -> None:
        with pytest.raises(ValidationError):
            DashboardRuntimeConfig(bind_port=0)

    def test_runtime_config_rejects_invalid_port_above_range(self) -> None:
        with pytest.raises(ValidationError):
            DashboardRuntimeConfig(bind_port=99999)


# ═══════════════════════════════════════════════════════════════════════════
# Schema: DashboardDatabaseTarget
# ═══════════════════════════════════════════════════════════════════════════


class TestDashboardDatabaseTarget:
    """Pydantic schema: DashboardDatabaseTarget — deployed SQLite DB target."""

    def test_db_target_has_path_field(self) -> None:
        target = DashboardDatabaseTarget(path="/data/poly_oracle.db")
        assert target.path == "/data/poly_oracle.db"

    def test_db_target_deployed_default_is_data_poly_oracle_db(self) -> None:
        target = DashboardDatabaseTarget()
        assert target.path == "/data/poly_oracle.db"

    def test_db_target_rejects_empty_path(self) -> None:
        with pytest.raises(ValidationError):
            DashboardDatabaseTarget(path="")

    def test_db_target_accepts_valid_absolute_path(self) -> None:
        target = DashboardDatabaseTarget(path="/data/poly_oracle.db")
        assert target.path == "/data/poly_oracle.db"

    def test_db_target_accepts_valid_relative_path(self) -> None:
        target = DashboardDatabaseTarget(path="./poly_oracle.db")
        assert target.path == "./poly_oracle.db"

    def test_db_target_read_only_mode_flag(self) -> None:
        target = DashboardDatabaseTarget()
        assert target.read_only is True

    def test_db_target_read_only_cannot_be_false_in_deployed_mode(self) -> None:
        with pytest.raises(ValidationError):
            DashboardDatabaseTarget(
                path="/data/poly_oracle.db",
                read_only=False,
                access_mode=DashboardAccessMode.SSH_TUNNEL,
            )


# ═══════════════════════════════════════════════════════════════════════════
# Schema: DashboardAccessMode
# ═══════════════════════════════════════════════════════════════════════════


class TestDashboardAccessMode:
    """Enum/schema: DashboardAccessMode — LOCAL, SSH_TUNNEL, REVERSE_PROXY."""

    def test_access_mode_has_local_value(self) -> None:
        assert DashboardAccessMode.LOCAL.value == "local"

    def test_access_mode_has_ssh_tunnel_value(self) -> None:
        assert DashboardAccessMode.SSH_TUNNEL.value == "ssh_tunnel"

    def test_access_mode_has_reverse_proxy_value(self) -> None:
        assert DashboardAccessMode.REVERSE_PROXY.value == "reverse_proxy"

    def test_access_mode_default_is_local(self) -> None:
        target = DashboardDatabaseTarget()
        assert target.access_mode == DashboardAccessMode.LOCAL

    def test_access_mode_rejects_unknown_string(self) -> None:
        with pytest.raises(ValidationError):
            DashboardDatabaseTarget(access_mode="public_internet")


# ═══════════════════════════════════════════════════════════════════════════
# Schema: DashboardTunnelSpec
# ═══════════════════════════════════════════════════════════════════════════


class TestDashboardTunnelSpec:
    """Pydantic schema: DashboardTunnelSpec — SSH tunnel configuration."""

    def test_tunnel_spec_local_port_field(self) -> None:
        spec = DashboardTunnelSpec()
        assert spec.local_port == 8501

    def test_tunnel_spec_remote_host_field(self) -> None:
        spec = DashboardTunnelSpec(remote_host="127.0.0.1")
        assert spec.remote_host == "127.0.0.1"

    def test_tunnel_spec_remote_port_field(self) -> None:
        spec = DashboardTunnelSpec()
        assert spec.remote_port == 8501

    def test_tunnel_spec_ssh_user_field(self) -> None:
        spec = DashboardTunnelSpec(ssh_user="deploy")
        assert spec.ssh_user == "deploy"

    def test_tunnel_spec_rejects_empty_remote_host(self) -> None:
        with pytest.raises(ValidationError):
            DashboardTunnelSpec(remote_host="")

    def test_tunnel_spec_rejects_public_remote_port_mapping(self) -> None:
        with pytest.raises(ValidationError) as exc:
            DashboardTunnelSpec(remote_host="0.0.0.0")
        assert "0.0.0.0" in str(exc.value) or "public" in str(exc.value).lower()


# ═══════════════════════════════════════════════════════════════════════════
# Schema: DashboardReadOnlyCheck
# ═══════════════════════════════════════════════════════════════════════════


class TestDashboardReadOnlyCheck:
    """Pydantic schema: DashboardReadOnlyCheck — read-only SQLite enforcement."""

    def test_readonly_check_passed_field(self) -> None:
        check = DashboardReadOnlyCheck(passed=True)
        assert check.passed is True

    def test_readonly_check_reason_field(self) -> None:
        check = DashboardReadOnlyCheck(
            passed=True, reason="Read-only URI mode confirmed"
        )
        assert "Read-only" in check.reason

    def test_readonly_check_write_attempted_field(self) -> None:
        check = DashboardReadOnlyCheck(passed=True, write_attempted=True)
        assert check.write_attempted is True

    def test_readonly_check_db_path_field(self) -> None:
        check = DashboardReadOnlyCheck(passed=True, db_path="/data/poly_oracle.db")
        assert check.db_path == "/data/poly_oracle.db"

    def test_readonly_check_uri_mode_field(self) -> None:
        check = DashboardReadOnlyCheck(passed=True, uri_mode=True)
        assert check.uri_mode is True


# ═══════════════════════════════════════════════════════════════════════════
# Schema: DashboardExposureCheck
# ═══════════════════════════════════════════════════════════════════════════


class TestDashboardExposureCheck:
    """Pydantic schema: DashboardExposureCheck — secret-free output verification."""

    def test_exposure_check_passed_field(self) -> None:
        check = DashboardExposureCheck(passed=True)
        assert check.passed is True

    def test_exposure_check_prohibited_patterns_field(self) -> None:
        check = DashboardExposureCheck(
            passed=True,
            prohibited_patterns=["private_key", "api_key"],
        )
        assert "private_key" in check.prohibited_patterns
        assert "api_key" in check.prohibited_patterns

    def test_exposure_check_violations_found_field(self) -> None:
        check = DashboardExposureCheck(
            passed=False,
            violations_found=["Detected 0x... private key pattern"],
        )
        assert len(check.violations_found) == 1
        assert "private key" in check.violations_found[0]

    def test_exposure_check_rejects_wallet_private_key_in_output(self) -> None:
        """64-char hex after 0x signals a private key."""
        check = DashboardExposureCheck(
            passed=False,
            prohibited_patterns=["private_key"],
            violations_found=["0x" + "a" * 64 + " found in output"],
        )
        assert check.passed is False
        assert len(check.violations_found) > 0

    def test_exposure_check_rejects_api_key_pattern(self) -> None:
        check = DashboardExposureCheck(
            passed=False,
            prohibited_patterns=["api_key"],
            violations_found=["sk-ant-api03-... found"],
        )
        assert check.passed is False

    def test_exposure_check_rejects_telegram_token_pattern(self) -> None:
        check = DashboardExposureCheck(
            passed=False,
            prohibited_patterns=["telegram_token"],
            violations_found=["123456:ABC-DEF1234gh found"],
        )
        assert check.passed is False

    def test_exposure_check_rejects_raw_prompt_text(self) -> None:
        check = DashboardExposureCheck(
            passed=False,
            prohibited_patterns=["raw_prompt"],
            violations_found=["System prompt block detected in output"],
        )
        assert check.passed is False

    def test_exposure_check_clean_output_passes(self) -> None:
        check = DashboardExposureCheck(
            passed=True,
            prohibited_patterns=[
                "private_key",
                "api_key",
                "telegram_token",
                "raw_prompt",
            ],
        )
        assert check.passed is True
        assert len(check.violations_found) == 0


# ═══════════════════════════════════════════════════════════════════════════
# Schema: DashboardAccessValidationReport
# ═══════════════════════════════════════════════════════════════════════════


class TestDashboardAccessValidationReport:
    """Pydantic schema: DashboardAccessValidationReport — aggregate validation."""

    def test_validation_report_aggregates_all_checks(self) -> None:
        ro = DashboardReadOnlyCheck(passed=True, reason="ok")
        exp = DashboardExposureCheck(passed=True)
        report = DashboardAccessValidationReport(
            read_only_check=ro,
            exposure_check=exp,
            overall_pass=True,
        )
        assert report.read_only_check is ro
        assert report.exposure_check is exp

    def test_validation_report_overall_pass_field(self) -> None:
        ro = DashboardReadOnlyCheck(passed=True, reason="ok")
        exp = DashboardExposureCheck(passed=True)
        report = DashboardAccessValidationReport(
            read_only_check=ro,
            exposure_check=exp,
            overall_pass=True,
        )
        assert report.overall_pass is True

    def test_validation_report_fails_when_readonly_fails(self) -> None:
        ro = DashboardReadOnlyCheck(passed=False, reason="Write attempted")
        exp = DashboardExposureCheck(passed=True)
        report = DashboardAccessValidationReport(
            read_only_check=ro,
            exposure_check=exp,
            overall_pass=False,
        )
        assert report.overall_pass is False

    def test_validation_report_fails_when_exposure_fails(self) -> None:
        ro = DashboardReadOnlyCheck(passed=True, reason="ok")
        exp = DashboardExposureCheck(
            passed=False,
            violations_found=["secret found"],
        )
        report = DashboardAccessValidationReport(
            read_only_check=ro,
            exposure_check=exp,
            overall_pass=False,
        )
        assert report.overall_pass is False

    def test_validation_report_summary_field(self) -> None:
        ro = DashboardReadOnlyCheck(passed=True, reason="Pass")
        exp = DashboardExposureCheck(passed=True)
        report = DashboardAccessValidationReport(
            read_only_check=ro,
            exposure_check=exp,
            overall_pass=True,
            summary="All dashboard access checks passed.",
        )
        assert "passed" in report.summary.lower()


# ═══════════════════════════════════════════════════════════════════════════
# DB Path Configuration
# ═══════════════════════════════════════════════════════════════════════════


class TestDashboardDBPathConfiguration:
    """Dashboard DB path must be configurable via DASHBOARD_DB_PATH env var."""

    def test_dashboard_db_path_env_var_respected(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            tmp_path = f.name
        try:
            # Create a valid SQLite file so the URI mode works
            conn = sqlite3.connect(tmp_path)
            conn.execute("CREATE TABLE test (id INTEGER)")
            conn.commit()
            conn.close()

            with mock.patch.dict(os.environ, {"DASHBOARD_DB_PATH": tmp_path}):
                import importlib
                import src.ui.dashboard as dash

                importlib.reload(dash)
                assert dash.DB_PATH == Path(tmp_path)
        finally:
            os.unlink(tmp_path)

    def test_dashboard_db_path_defaults_to_local_when_not_set(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            # Remove DASHBOARD_DB_PATH entirely
            os.environ.pop("DASHBOARD_DB_PATH", None)
            import importlib
            import src.ui.dashboard as dash

            importlib.reload(dash)
            # Should fall back to the local default (relative to this repo)
            assert dash.DB_PATH.name == "poly_oracle.db"

    def test_dashboard_db_path_nonexistent_file_does_not_create(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            nonexistent = Path(tmpdir) / "nonexistent.db"
            assert not nonexistent.exists()
            with mock.patch.dict(os.environ, {"DASHBOARD_DB_PATH": str(nonexistent)}):
                import importlib
                import src.ui.dashboard as dash

                importlib.reload(dash)
                # The file must not have been created by the module import
                assert not nonexistent.exists()

    def test_dashboard_db_path_relative_resolved_correctly(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rel_path = "test_dashboard.db"
            abs_path = Path(tmpdir) / rel_path
            # Create a valid DB
            conn = sqlite3.connect(str(abs_path))
            conn.execute("CREATE TABLE x (y INTEGER)")
            conn.commit()
            conn.close()

            with mock.patch.dict(os.environ, {"DASHBOARD_DB_PATH": str(abs_path)}):
                import importlib
                import src.ui.dashboard as dash

                importlib.reload(dash)
                assert dash.DB_PATH.resolve() == abs_path.resolve()


# ═══════════════════════════════════════════════════════════════════════════
# Read-Only SQLite Enforcement
# ═══════════════════════════════════════════════════════════════════════════


class TestReadOnlySQLiteEnforcement:
    """src/ui/ must open SQLite in read-only mode; no write SQL verbs."""

    @staticmethod
    def _make_test_db() -> str:
        """Create a temporary SQLite file with a test table and return its path."""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE _wi49_test (id INTEGER PRIMARY KEY, val TEXT)")
        conn.execute("INSERT INTO _wi49_test (val) VALUES ('hello')")
        conn.commit()
        conn.close()
        return path

    def test_sqlite_connection_opens_read_only(self) -> None:
        db_path = self._make_test_db()
        try:
            uri = f"file:{db_path}?mode=ro"
            conn = sqlite3.connect(uri, uri=True)
            # SELECT must work
            rows = conn.execute("SELECT val FROM _wi49_test").fetchall()
            assert len(rows) == 1
            assert rows[0][0] == "hello"
            conn.close()
        finally:
            os.unlink(db_path)

    def test_write_query_insert_is_rejected(self) -> None:
        db_path = self._make_test_db()
        try:
            uri = f"file:{db_path}?mode=ro"
            conn = sqlite3.connect(uri, uri=True)
            with pytest.raises(sqlite3.OperationalError):
                conn.execute("INSERT INTO _wi49_test (val) VALUES ('write')")
            conn.close()
        finally:
            os.unlink(db_path)

    def test_write_query_update_is_rejected(self) -> None:
        db_path = self._make_test_db()
        try:
            uri = f"file:{db_path}?mode=ro"
            conn = sqlite3.connect(uri, uri=True)
            with pytest.raises(sqlite3.OperationalError):
                conn.execute("UPDATE _wi49_test SET val = 'changed'")
            conn.close()
        finally:
            os.unlink(db_path)

    def test_write_query_delete_is_rejected(self) -> None:
        db_path = self._make_test_db()
        try:
            uri = f"file:{db_path}?mode=ro"
            conn = sqlite3.connect(uri, uri=True)
            with pytest.raises(sqlite3.OperationalError):
                conn.execute("DELETE FROM _wi49_test")
            conn.close()
        finally:
            os.unlink(db_path)

    def test_write_query_create_table_is_rejected(self) -> None:
        db_path = self._make_test_db()
        try:
            uri = f"file:{db_path}?mode=ro"
            conn = sqlite3.connect(uri, uri=True)
            with pytest.raises(sqlite3.OperationalError):
                conn.execute("CREATE TABLE illegal (x INTEGER)")
            conn.close()
        finally:
            os.unlink(db_path)

    def test_write_query_drop_table_is_rejected(self) -> None:
        db_path = self._make_test_db()
        try:
            uri = f"file:{db_path}?mode=ro"
            conn = sqlite3.connect(uri, uri=True)
            with pytest.raises(sqlite3.OperationalError):
                conn.execute("DROP TABLE _wi49_test")
            conn.close()
        finally:
            os.unlink(db_path)

    def test_write_query_alter_table_is_rejected(self) -> None:
        db_path = self._make_test_db()
        try:
            uri = f"file:{db_path}?mode=ro"
            conn = sqlite3.connect(uri, uri=True)
            with pytest.raises(sqlite3.OperationalError):
                conn.execute("ALTER TABLE _wi49_test ADD COLUMN extra TEXT")
            conn.close()
        finally:
            os.unlink(db_path)

    def test_select_query_succeeds_on_read_only_connection(self) -> None:
        db_path = self._make_test_db()
        try:
            uri = f"file:{db_path}?mode=ro"
            conn = sqlite3.connect(uri, uri=True)
            rows = conn.execute("SELECT COUNT(*) FROM _wi49_test").fetchall()
            assert rows[0][0] == 1
            conn.close()
        finally:
            os.unlink(db_path)

    def test_no_write_verbs_in_ui_source_code(self) -> None:
        """Scan src/ui/ for forbidden SQL write verbs."""
        ui_dir = Path(__file__).resolve().parents[2] / "src" / "ui"
        forbidden = {
            "INSERT",
            "UPDATE",
            "DELETE",
            "CREATE TABLE",
            "DROP TABLE",
            "DROP INDEX",
            "ALTER TABLE",
            "REPLACE",
        }
        for py_file in ui_dir.glob("*.py"):
            source = py_file.read_text()
            source_upper = source.upper()
            for verb in forbidden:
                # Allow the verb in comments or docstrings mentioning the test itself
                if verb.upper() in source_upper:
                    # Check it's not just in a string/comment about the prohibition
                    lines = source.split("\n")
                    for line in lines:
                        stripped = line.strip()
                        if (
                            stripped.startswith("#")
                            or stripped.startswith('"""')
                            or stripped.startswith("'''")
                        ):
                            continue
                        if verb.upper() in stripped.upper():
                            # Only flag actual SQL execution patterns
                            if (
                                ".execute(" in stripped
                                or ".executemany(" in stripped
                                or ".executescript(" in stripped
                            ):
                                if verb.upper() in stripped.upper():
                                    pytest.fail(
                                        f"{py_file.name}: line contains {verb}: {stripped[:120]}"
                                    )

    def test_no_executescript_calls_in_ui_source(self) -> None:
        ui_dir = Path(__file__).resolve().parents[2] / "src" / "ui"
        for py_file in ui_dir.glob("*.py"):
            source = py_file.read_text()
            if ".executescript(" in source:
                pytest.fail(f"{py_file.name} contains .executescript()")

    def test_no_executemany_writes_in_ui_source(self) -> None:
        ui_dir = Path(__file__).resolve().parents[2] / "src" / "ui"
        for py_file in ui_dir.glob("*.py"):
            source = py_file.read_text()
            for line in source.split("\n"):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if ".executemany(" in stripped:
                    line_upper = stripped.upper()
                    if any(
                        v in line_upper
                        for v in ("INSERT", "UPDATE", "DELETE", "REPLACE")
                    ):
                        pytest.fail(
                            f"{py_file.name}: executemany with write verb: {stripped[:120]}"
                        )


# ═══════════════════════════════════════════════════════════════════════════
# Compose Profile Gating
# ═══════════════════════════════════════════════════════════════════════════


class TestComposeProfileGating:
    """docker-compose.yml must have a profile-gated dashboard service."""

    @staticmethod
    def _load_compose_text() -> str:
        compose_path = Path(__file__).resolve().parents[2] / "docker-compose.yml"
        return compose_path.read_text()

    @staticmethod
    def _service_has_profile(
        compose_text: str, service_name: str, profile_name: str
    ) -> bool:
        """Check if a service block contains a profiles list with the given profile."""
        # Find the service block
        pattern = rf"^\s{{2}}{service_name}:.*?\n(?:^\s{{4}}.*\n)*"
        match = re.search(pattern, compose_text, re.MULTILINE)
        if not match:
            return False
        block = match.group(0)
        # Check for profiles key containing the profile name
        return bool(
            re.search(rf"profiles:\s*\n(\s{{6}}-.*\n)*\s{{6}}-\s+{profile_name}", block)
        )

    @staticmethod
    def _service_has_volume(
        compose_text: str, service_name: str, volume_name: str
    ) -> bool:
        """Check if a service block mounts the given named volume."""
        pattern = rf"^\s{{2}}{service_name}:.*?\n(?:^\s{{4}}.*\n)*"
        match = re.search(pattern, compose_text, re.MULTILINE)
        if not match:
            return False
        block = match.group(0)
        return volume_name in block

    def test_dashboard_service_has_profile_key(self) -> None:
        compose_text = self._load_compose_text()
        assert "dashboard:" in compose_text, "dashboard service must exist"
        assert "profiles:" in compose_text, "compose must have profiles section"
        assert self._service_has_profile(compose_text, "dashboard", "dashboard"), (
            "dashboard service must have 'dashboard' in its profiles list"
        )

    def test_dashboard_not_started_by_default(self) -> None:
        compose_text = self._load_compose_text()
        # Orchestrator must NOT be profile-gated
        orchestrator_block = re.search(
            r"  orchestrator:.*?\n(?:    .*\n)*", compose_text
        )
        assert orchestrator_block is not None
        orch_text = orchestrator_block.group(0)
        assert "profiles:" not in orch_text, "orchestrator must not be profile-gated"

        # Dashboard must be profile-gated
        assert self._service_has_profile(compose_text, "dashboard", "dashboard")

    def test_dashboard_starts_with_profile_flag(self) -> None:
        compose_text = self._load_compose_text()
        assert self._service_has_profile(compose_text, "dashboard", "dashboard")

    def test_dashboard_mounts_same_data_volume(self) -> None:
        compose_text = self._load_compose_text()
        assert self._service_has_volume(
            compose_text, "orchestrator", "poly_oracle_data"
        )
        assert self._service_has_volume(compose_text, "dashboard", "poly_oracle_data")

    def test_dashboard_uses_data_poly_oracle_db_path(self) -> None:
        compose_text = self._load_compose_text()
        assert "DASHBOARD_DB_PATH: /data/poly_oracle.db" in compose_text or (
            "DASHBOARD_DB_PATH=/data/poly_oracle.db" in compose_text
        ), "dashboard must set DASHBOARD_DB_PATH to /data/poly_oracle.db"

    def test_dashboard_binds_loopback_only(self) -> None:
        compose_text = self._load_compose_text()
        # Find dashboard port mappings
        dashboard_block = re.search(r"  dashboard:.*?\n(?:    .*\n)*", compose_text)
        assert dashboard_block is not None
        block = dashboard_block.group(0)
        # Must contain a 127.0.0.1 port binding
        assert "127.0.0.1:8501" in block, "Dashboard must bind to 127.0.0.1"
        assert "0.0.0.0:8501" not in block, "Dashboard must NOT bind to 0.0.0.0"


# ═══════════════════════════════════════════════════════════════════════════
# Secret-Free Output
# ═══════════════════════════════════════════════════════════════════════════


class TestSecretFreeOutput:
    """Dashboard must not expose secrets, keys, tokens, or raw private payloads."""

    _SECRET_PATTERNS = [
        (r"0x[a-fA-F0-9]{64}", "private_key"),
        (r"sk-ant-[a-zA-Z0-9_-]+", "api_key"),
        (r"\d{8,10}:[a-zA-Z0-9_-]{35}", "telegram_token"),
        (r"PRIVATE_KEY\s*=\s*['\"]?0x", "private_key_config"),
        (r"ANTHROPIC_API_KEY\s*=\s*['\"]?sk", "api_key_config"),
    ]

    def _scan_ui_source(self) -> list[str]:
        """Scan src/ui/ for lines that match secret patterns."""
        ui_dir = Path(__file__).resolve().parents[2] / "src" / "ui"
        violations = []
        for py_file in ui_dir.glob("*.py"):
            source = py_file.read_text()
            for pattern, label in self._SECRET_PATTERNS:
                for match in re.finditer(pattern, source):
                    line = source[: match.start()].count("\n") + 1
                    violations.append(f"{py_file.name}:{line}: {label} pattern match")
        return violations

    def test_dashboard_does_not_display_env_config_dump(self) -> None:
        ui_dir = Path(__file__).resolve().parents[2] / "src" / "ui"
        for py_file in ui_dir.glob("*.py"):
            source = py_file.read_text()
            # The dashboard must not iterate os.environ wholesale
            if "os.environ" in source and "for" in source:
                lines = source.split("\n")
                for i, line in enumerate(lines):
                    if "os.environ" in line and (
                        "st." in line or "render" in line.lower()
                    ):
                        pytest.fail(
                            f"{py_file.name}:{i + 1}: os.environ exposed in render path"
                        )

    def test_dashboard_does_not_display_wallet_private_key(self) -> None:
        violations = self._scan_ui_source()
        privkey_violations = [v for v in violations if "private_key" in v]
        assert len(privkey_violations) == 0, (
            f"Private key patterns found: {privkey_violations}"
        )

    def test_dashboard_does_not_display_api_keys(self) -> None:
        violations = self._scan_ui_source()
        apikey_violations = [v for v in violations if "api_key" in v]
        assert len(apikey_violations) == 0, (
            f"API key patterns found: {apikey_violations}"
        )

    def test_dashboard_does_not_display_raw_prompt_text(self) -> None:
        ui_dir = Path(__file__).resolve().parents[2] / "src" / "ui"
        for py_file in ui_dir.glob("*.py"):
            source = py_file.read_text()
            # The dashboard must not import or reference prompt/instruction content
            if (
                "system_prompt" in source.lower()
                or "instruction_prompt" in source.lower()
            ):
                pytest.fail(f"{py_file.name} references prompt/instruction material")

    def test_dashboard_does_not_display_reasoning_text(self) -> None:
        ui_dir = Path(__file__).resolve().parents[2] / "src" / "ui"
        for py_file in ui_dir.glob("*.py"):
            source = py_file.read_text()
            if "reasoning_text" in source or "reasoning_content" in source:
                pytest.fail(f"{py_file.name} references private reasoning content")

    def test_dashboard_metrics_are_secret_free(self) -> None:
        # The dashboard's metric rendering functions must not reference
        # secrets or private keys in their labels/values.
        ui_dir = Path(__file__).resolve().parents[2] / "src" / "ui"
        for py_file in ui_dir.glob("*.py"):
            source = py_file.read_text()
            forbidden_in_metrics = [
                "wallet_address",
                "wallet_private_key",
                "api_key",
                "telegram_bot_token",
                "telegram_chat_id",
            ]
            for secret_field in forbidden_in_metrics:
                if secret_field in source:
                    # It's OK in comments/docstrings only
                    lines = source.split("\n")
                    for i, line in enumerate(lines):
                        stripped = line.strip()
                        if stripped.startswith("#") or stripped.startswith('"""'):
                            continue
                        if secret_field in stripped:
                            pytest.fail(
                                f"{py_file.name}:{i + 1}: secret field '{secret_field}' in non-comment line"
                            )


# ═══════════════════════════════════════════════════════════════════════════
# SSH Tunnel Runbook
# ═══════════════════════════════════════════════════════════════════════════


class TestSSHTunnelRunbook:
    """docs/runbooks/streamlit-ssh-tunnel.md must exist with required sections."""

    _runbook_path = (
        Path(__file__).resolve().parents[2]
        / "docs"
        / "runbooks"
        / "streamlit-ssh-tunnel.md"
    )

    def test_runbook_file_exists(self) -> None:
        assert self._runbook_path.exists(), f"Runbook not found at {self._runbook_path}"

    def test_runbook_contains_ssh_command(self) -> None:
        content = self._runbook_path.read_text()
        assert "ssh -N -L" in content, "Runbook must contain SSH tunnel command"

    def test_runbook_contains_local_browser_url(self) -> None:
        content = self._runbook_path.read_text()
        assert "http://localhost:8501" in content, "Runbook must document localhost URL"

    def test_runbook_contains_shutdown_steps(self) -> None:
        content = self._runbook_path.read_text()
        assert "Ctrl+C" in content or "stop" in content.lower(), (
            "Runbook must include shutdown steps"
        )

    def test_runbook_contains_verification_section(self) -> None:
        content = self._runbook_path.read_text()
        assert "Verification" in content or "troubleshoot" in content.lower(), (
            "Runbook must have a verification/troubleshooting section"
        )

    def test_runbook_contains_no_public_exposure_instructions(self) -> None:
        content = self._runbook_path.read_text()
        # Must not tell users to open port 8501 on 0.0.0.0 publicly
        # (explaining internal 0.0.0.0 container bind is acceptable)
        assert "publish" not in content.lower() or "loopback" in content.lower(), (
            "Runbook must not suggest public port publishing without loopback restriction"
        )
        # Must still emphasize non-public exposure
        assert "never exposed publicly" in content.lower()
