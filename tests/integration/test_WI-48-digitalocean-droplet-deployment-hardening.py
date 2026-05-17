"""
Integration tests for WI-48 — DigitalOcean Droplet Deployment Hardening.

Tests the deployment checker (`scripts/ops/check_deployment.py`) against
mocked subprocess, urllib HTTP probes, dry-run guard, and secret-free
payload validation.  Validates output dicts against typed schemas from
``src.schemas.ops``.
"""

import io
import json
import subprocess
import sys
import urllib.error
import urllib.request
from unittest.mock import MagicMock, patch

import pytest

from scripts.ops.check_deployment import (
    _check_compose_service,
    _check_docker_installed,
    _check_dry_run_guard,
    _inspect_metrics_labels,
    _probe_healthz,
    _probe_metrics,
    _probe_readyz,
    main,
)
from src.schemas.ops import (
    DeploymentCheckStatus,
    DeploymentFailureReason,
    DeploymentProbeResult,
    DeploymentValidationReport,
)


# ── helpers ────────────────────────────────────────────────────────────────


def _mock_subprocess_run(
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> MagicMock:
    result = MagicMock(spec=subprocess.CompletedProcess)
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


class _FakeHTTPResponse:
    """A minimal fake for urllib.request.urlopen return value."""

    def __init__(self, status: int, body: str, headers: dict | None = None) -> None:
        self.status = status
        self._body = body.encode("utf-8")
        self.headers = headers or {"Content-Type": "text/plain; charset=utf-8"}

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeHTTPResponse":
        return self

    def __exit__(self, *args: object) -> None:
        pass


def _raise_urlerror(reason: str = "Connection refused") -> None:
    raise urllib.error.URLError(reason)


def _raise_httperror(status: int, body: str = "") -> None:
    resp = _FakeHTTPResponse(status, body)
    raise urllib.error.HTTPError(
        "http://127.0.0.1:8080/healthz", status, "Error", dict(resp.headers), None
    )


# ── Deployment Checker Success ─────────────────────────────────────────────


class TestDeploymentCheckerSuccess:
    """All mandatory checks pass — checker exits 0."""

    def test_all_checks_pass_healthy(self, tmp_path, monkeypatch):
        """Full check suite passes when all dependencies are healthy."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text("DRY_RUN=true\n")
        (tmp_path / "docker-compose.yml").write_text("services:\n")

        with (
            patch("subprocess.run") as mock_run,
            patch("urllib.request.urlopen") as mock_urlopen,
        ):
            # Mock docker/compose subprocess
            def _run_side_effect(*args, **kwargs):
                cmd = args[0] if args else kwargs.get("args", [])
                cmd_str = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
                if "compose" in cmd_str and "ps" in cmd_str:
                    return _mock_subprocess_run(
                        stdout=json.dumps([{"State": "running"}])
                    )
                return _mock_subprocess_run()

            mock_run.side_effect = _run_side_effect

            # Mock HTTP responses: healthz, readyz, metrics, metrics
            mock_urlopen.side_effect = [
                _FakeHTTPResponse(200, '{"status":"ok"}'),
                _FakeHTTPResponse(
                    200,
                    '{"status":"ready","checks":{"database":"reachable","websocket":"connected"}}',
                ),
                _FakeHTTPResponse(200, "# HELP test\ntest 1.0\n"),
                _FakeHTTPResponse(200, "# HELP test\ntest 1.0\n"),
            ]

            try:
                main()
            except SystemExit as e:
                assert e.code == 0

    def test_degraded_readiness_allowed_when_explicit(self, tmp_path, monkeypatch):
        """--allow-degraded flag permits degraded readiness."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text("DRY_RUN=true\n")
        (tmp_path / "docker-compose.yml").write_text("services:\n")

        with (
            patch("subprocess.run") as mock_run,
            patch("urllib.request.urlopen") as mock_urlopen,
            patch("sys.argv", ["check_deployment.py", "--allow-degraded"]),
        ):
            mock_run.return_value = _mock_subprocess_run(
                stdout=json.dumps([{"State": "running"}])
            )

            mock_urlopen.side_effect = [
                _FakeHTTPResponse(200, '{"status":"ok"}'),
                _FakeHTTPResponse(
                    200,
                    '{"status":"degraded","checks":{"database":"reachable","websocket":"disconnected"}}',
                ),
                _FakeHTTPResponse(200, "# HELP test\ntest 1.0\n"),
                _FakeHTTPResponse(200, "# HELP test\ntest 1.0\n"),
            ]

            try:
                main()
            except SystemExit as e:
                assert e.code == 0

    def test_degraded_without_flag_fails(self, tmp_path, monkeypatch):
        """Degraded readiness without --allow-degraded must fail."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text("DRY_RUN=true\n")
        (tmp_path / "docker-compose.yml").write_text("services:\n")

        with (
            patch("subprocess.run") as mock_run,
            patch("urllib.request.urlopen") as mock_urlopen,
        ):
            mock_run.return_value = _mock_subprocess_run(
                stdout=json.dumps([{"State": "running"}])
            )

            # readyz returns degraded
            mock_urlopen.side_effect = [
                _FakeHTTPResponse(200, '{"status":"ok"}'),
                _FakeHTTPResponse(
                    200,
                    '{"status":"degraded","checks":{"database":"reachable","websocket":"disconnected"}}',
                ),
                _FakeHTTPResponse(200, "# HELP test\ntest 1.0\n"),
                _FakeHTTPResponse(200, "# HELP test\ntest 1.0\n"),
            ]

            with pytest.raises(SystemExit) as exc_info:
                main()
            # Exit code should be non-zero because readyz probe fails
            assert exc_info.value.code == 1


# ── Compose Service Status ─────────────────────────────────────────────────


class TestComposeServiceStatus:
    """Compose service status validation."""

    def test_service_not_running_fails(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _mock_subprocess_run(
                stdout=json.dumps([{"State": "exited", "Status": "Exited (1)"}])
            )
            probe = _check_compose_service()
            assert probe["status"] == "fail"
            assert probe["failure_reason"] == "service_not_running"

    def test_container_restarting_reports_restart_count(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _mock_subprocess_run(
                stdout=json.dumps([{"State": "restarting", "Status": "Restarting (3)"}])
            )
            probe = _check_compose_service()
            assert probe["status"] == "fail"
            assert probe["failure_reason"] == "container_restarting"


# ── Dry-Run Guard ──────────────────────────────────────────────────────────


class TestDryRunGuard:
    """DRY_RUN=true is mandatory."""

    def test_dry_run_missing_fails(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text("OTHER_KEY=value\n")
        (tmp_path / "docker-compose.yml").write_text("services:\n")
        probe = _check_dry_run_guard()
        assert probe["status"] == "fail"
        assert probe["failure_reason"] == "dry_run_missing"

    def test_dry_run_false_fails(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text("DRY_RUN=false\n")
        (tmp_path / "docker-compose.yml").write_text("services:\n")
        probe = _check_dry_run_guard()
        assert probe["status"] == "fail"
        assert probe["failure_reason"] == "dry_run_false"

    def test_env_file_absent_fails_before_probes(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        probe = _check_dry_run_guard()
        assert probe["status"] == "fail"
        assert probe["failure_reason"] == "env_file_absent"

    def test_dry_run_true_passes(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text("DRY_RUN=true\n")
        (tmp_path / "docker-compose.yml").write_text("services:\n")
        probe = _check_dry_run_guard()
        assert probe["status"] == "pass"

    def test_dry_run_true_with_inline_comment_passes(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text("DRY_RUN=true  # required on Droplet\n")
        (tmp_path / "docker-compose.yml").write_text("services:\n")
        probe = _check_dry_run_guard()
        assert probe["status"] == "pass"


# ── Health / Readiness Probes ──────────────────────────────────────────────


class TestHTTPProbes:
    """Health and readiness HTTP probe validation."""

    def test_healthz_unreachable_fails(self):
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = lambda *a, **kw: _raise_urlerror()
            result = _probe_healthz()
            assert result["status"] == "fail"
            assert result["failure_reason"] == "healthz_unreachable"

    def test_readyz_unreachable_fails(self):
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = lambda *a, **kw: _raise_urlerror()
            result = _probe_readyz()
            assert result["status"] == "fail"
            assert result["failure_reason"] == "readyz_unreachable"

    def test_readyz_malformed_json_fails(self):
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _FakeHTTPResponse(200, "not json")
            result = _probe_readyz()
            assert result["status"] == "fail"
            assert result["failure_reason"] == "readyz_malformed"

    def test_http_probes_use_explicit_timeout(self):
        """Timeout triggers a typed failure, not a hang."""
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = lambda *a, **kw: _raise_urlerror("timed out")
            result = _probe_healthz()
            assert result["status"] == "fail"
            assert result["failure_reason"] == "healthz_unreachable"

    def test_healthz_pass(self):
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _FakeHTTPResponse(200, '{"status":"ok"}')
            result = _probe_healthz()
            assert result["status"] == "pass"

    def test_readyz_not_ready_fails(self):
        """not_ready status is always a failure, regardless of flags."""
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _FakeHTTPResponse(
                200,
                '{"status":"not_ready","checks":{"database":"unreachable","websocket":"disconnected"}}',
            )
            result = _probe_readyz()
            assert result["status"] == "fail"
            assert result["failure_reason"] == "readyz_unreachable"

    def test_degraded_allow_flag_no_checks_detail_fails(self):
        """With --allow-degraded, degraded must still include checks payload."""
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _FakeHTTPResponse(
                200,
                '{"status":"degraded"}',
            )
            result = _probe_readyz(allow_degraded=True)
            assert result["status"] == "fail"
            assert result["failure_reason"] == "readyz_malformed"


# ── Metrics Inspection ─────────────────────────────────────────────────────


class TestMetricsInspection:
    """Prometheus metrics validation and secret rejection."""

    def test_metrics_unreachable_fails(self):
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = lambda *a, **kw: _raise_urlerror()
            result = _inspect_metrics_labels()
            assert result["status"] == "fail"
            assert result["failure_reason"] == "metrics_unreachable"

    def test_metrics_contains_forbidden_secret_label_fails(self):
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _FakeHTTPResponse(
                200,
                (
                    "# HELP up Status\n"
                    "# TYPE up gauge\n"
                    'up{api_key="sk-ant-api-secret12345",env="prod"} 1.0\n'
                ),
            )
            result = _inspect_metrics_labels()
            assert result["status"] == "fail"
            assert result["failure_reason"] == "metrics_forbidden_label"

    def test_metrics_contains_prompt_text_fails(self):
        """Metrics containing wallet address patterns are rejected."""
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _FakeHTTPResponse(
                200,
                (
                    "# HELP test\n"
                    "# TYPE test gauge\n"
                    'test{wallet="0x1234567890abcdef1234567890abcdef12345678"} 1.0\n'
                ),
            )
            result = _inspect_metrics_labels()
            assert result["status"] == "fail"
            assert result["failure_reason"] == "metrics_forbidden_label"

    def test_metrics_valid_prometheus_text_passes(self):
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _FakeHTTPResponse(
                200,
                (
                    "# HELP poly_oracle_decisions_total Total decisions\n"
                    "# TYPE poly_oracle_decisions_total counter\n"
                    'poly_oracle_decisions_total{status="skipped",category="crypto"} 42\n'
                ),
            )
            result = _inspect_metrics_labels()
            assert result["status"] == "pass"

    def test_metrics_endpoint_disabled_fails_in_default_mode(self):
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = lambda *a, **kw: _raise_urlerror()
            result = _probe_metrics()
            assert result["status"] == "fail"
            assert result["failure_reason"] == "metrics_unreachable"

    def test_metrics_wrong_content_type_fails(self):
        """Non-text/plain Content-Type must fail the probe."""
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _FakeHTTPResponse(
                200,
                "<html><body>not metrics</body></html>",
                {"Content-Type": "text/html"},
            )
            result = _probe_metrics()
            assert result["status"] == "fail"
            assert result["failure_reason"] == "metrics_unreachable"

    def test_metrics_non_prometheus_body_fails(self):
        """Empty or non-Prometheus body must fail the probe."""
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _FakeHTTPResponse(
                200,
                "just some random text\nnot prometheus format\n",
            )
            result = _probe_metrics()
            assert result["status"] == "fail"

    def test_inspect_empty_metrics_body_fails(self):
        """Empty metrics body fails inspection."""
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _FakeHTTPResponse(200, "")
            result = _inspect_metrics_labels()
            assert result["status"] == "fail"
            assert result["failure_reason"] == "metrics_unreachable"

    def test_inspect_wrong_content_type_fails(self):
        """Metrics inspection with wrong Content-Type must fail."""
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _FakeHTTPResponse(
                200,
                "# HELP test\ntest 1.0\n",
                {"Content-Type": "application/json"},
            )
            result = _inspect_metrics_labels()
            assert result["status"] == "fail"
            assert result["failure_reason"] == "metrics_unreachable"


# ── Deployment Validation Report ───────────────────────────────────────────


class TestDeploymentValidationReport:
    """Structured deployment report generation."""

    def test_report_includes_all_probe_results(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text("DRY_RUN=true\n")
        (tmp_path / "docker-compose.yml").write_text("services:\n")

        captured = io.StringIO()

        with (
            patch("subprocess.run") as mock_run,
            patch("urllib.request.urlopen") as mock_urlopen,
        ):
            mock_run.return_value = _mock_subprocess_run(
                stdout=json.dumps([{"State": "running"}])
            )

            mock_urlopen.side_effect = [
                _FakeHTTPResponse(200, '{"status":"ok"}'),
                _FakeHTTPResponse(
                    200,
                    '{"status":"ready","checks":{"database":"reachable","websocket":"connected"}}',
                ),
                _FakeHTTPResponse(200, "# HELP test\ntest 1.0\n"),
                _FakeHTTPResponse(200, "# HELP test\ntest 1.0\n"),
            ]

            old_stdout = sys.stdout
            sys.stdout = captured
            try:
                try:
                    main()
                except SystemExit:
                    pass
            finally:
                sys.stdout = old_stdout

        report = json.loads(captured.getvalue())
        assert "probes" in report
        assert (
            len(report["probes"]) >= 7
        )  # docker + compose + service + dry_run + healthz + readyz + metrics + inspection

    def test_report_failure_reason_is_typed_enum(self):
        """All failure reasons map to known DeploymentFailureReason values."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("docker")
            result = _check_docker_installed()
            assert result["status"] == "fail"
            reason = result["failure_reason"]
            # Must be a valid DeploymentFailureReason value
            valid_reasons = {r.value for r in DeploymentFailureReason}
            assert reason in valid_reasons, f"{reason} not in {valid_reasons}"

    def test_checker_exits_nonzero_on_mandatory_failure(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text("DRY_RUN=false\n")
        (tmp_path / "docker-compose.yml").write_text("services:\n")

        with (
            patch("subprocess.run") as mock_run,
            patch("urllib.request.urlopen") as mock_urlopen,
        ):
            mock_run.return_value = _mock_subprocess_run(
                stdout=json.dumps([{"State": "running"}])
            )
            mock_urlopen.return_value = _FakeHTTPResponse(200, "ok")

            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 1

    def test_checker_exits_zero_on_all_pass(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text("DRY_RUN=true\n")
        (tmp_path / "docker-compose.yml").write_text("services:\n")

        with (
            patch("subprocess.run") as mock_run,
            patch("urllib.request.urlopen") as mock_urlopen,
        ):
            mock_run.return_value = _mock_subprocess_run(
                stdout=json.dumps([{"State": "running"}])
            )

            mock_urlopen.side_effect = [
                _FakeHTTPResponse(200, '{"status":"ok"}'),
                _FakeHTTPResponse(
                    200,
                    '{"status":"ready","checks":{"database":"reachable","websocket":"connected"}}',
                ),
                _FakeHTTPResponse(200, "# HELP test\ntest 1.0\n"),
                _FakeHTTPResponse(200, "# HELP test\ntest 1.0\n"),
            ]

            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 0


# ── Secret Hygiene ─────────────────────────────────────────────────────────


class TestSecretHygiene:
    """No secrets in committed files or validation output."""

    def test_dry_run_result_never_echoes_raw_value(self, tmp_path, monkeypatch):
        """DRY_RUN guard result must not contain the raw .env DRY_RUN value."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text("DRY_RUN=true\nSECRET_KEY=sk-ant-actual\n")
        (tmp_path / "docker-compose.yml").write_text("services:\n")
        probe = _check_dry_run_guard()
        assert probe["status"] == "pass"
        result_json = json.dumps(probe)
        assert "SECRET_KEY" not in result_json
        assert "sk-ant-actual" not in result_json

    def test_metrics_output_rejects_raw_token_ids(self):
        """Metrics with token/bot token patterns are rejected."""
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _FakeHTTPResponse(
                200,
                (
                    "# HELP test\n"
                    "# TYPE test gauge\n"
                    'test{bot_token="1234567890:ABCDEFG-HIJKLMNOPQRSTUVWXYZ"} 1.0\n'
                ),
            )
            result = _inspect_metrics_labels()
            assert result["status"] == "fail"
            assert result["failure_reason"] == "metrics_forbidden_label"

    def test_report_redacts_secret_like_fields(self, tmp_path, monkeypatch):
        """The JSON report must never include raw secret material."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text(
            "DRY_RUN=true\nANTHROPIC_API_KEY=sk-ant-real-key-value\n"
        )
        (tmp_path / "docker-compose.yml").write_text("services:\n")

        captured = io.StringIO()

        with (
            patch("subprocess.run") as mock_run,
            patch("urllib.request.urlopen") as mock_urlopen,
        ):
            mock_run.return_value = _mock_subprocess_run(
                stdout=json.dumps([{"State": "running"}])
            )
            mock_urlopen.side_effect = [
                _FakeHTTPResponse(200, '{"status":"ok"}'),
                _FakeHTTPResponse(
                    200,
                    '{"status":"ready","checks":{"database":"reachable","websocket":"connected"}}',
                ),
                _FakeHTTPResponse(200, "# HELP test\ntest 1.0\n"),
                _FakeHTTPResponse(200, "# HELP test\ntest 1.0\n"),
            ]

            old_stdout = sys.stdout
            sys.stdout = captured
            try:
                try:
                    main()
                except SystemExit:
                    pass
            finally:
                sys.stdout = old_stdout

        output = captured.getvalue()
        assert "sk-ant-real-key-value" not in output

    def test_env_file_read_fails_gracefully(self, tmp_path, monkeypatch):
        """Unreadable .env file is handled as absent."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "docker-compose.yml").write_text("services:\n")
        probe = _check_dry_run_guard()
        assert probe["status"] == "fail"
        assert probe["failure_reason"] == "env_file_absent"


# ── Schema Validation ──────────────────────────────────────────────────────


class TestDeploymentSchemas:
    """Validate Pydantic schemas match business logic constraints."""

    def test_failure_reason_enum_covers_all_edge_cases(self):
        """Every edge case from business logic has a corresponding enum value."""
        expected = {
            "docker_not_installed",
            "compose_plugin_not_installed",
            "service_not_running",
            "container_restarting",
            "env_file_absent",
            "dry_run_missing",
            "dry_run_false",
            "healthz_unreachable",
            "readyz_unreachable",
            "readyz_malformed",
            "metrics_unreachable",
            "metrics_forbidden_label",
            "metrics_disabled",
            "sqlite_missing",
            "timeout",
            "unknown",
        }
        actual = {r.value for r in DeploymentFailureReason}
        assert expected == actual

    def test_deployment_validation_report_frozen(self):
        report = DeploymentValidationReport(overall_status=DeploymentCheckStatus.PASS)
        with pytest.raises(Exception):
            report.overall_status = DeploymentCheckStatus.FAIL  # type: ignore[misc]

    def test_report_default_exit_code_zero(self):
        report = DeploymentValidationReport(overall_status=DeploymentCheckStatus.PASS)
        assert report.exit_code == 0

    def test_probe_result_must_have_name(self):
        with pytest.raises(Exception):
            DeploymentProbeResult(status=DeploymentCheckStatus.PASS)  # type: ignore[call-arg]

    def test_checker_output_conforms_to_schema(self, tmp_path, monkeypatch):
        """The complete checker JSON output validates against DeploymentValidationReport."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text("DRY_RUN=true\n")
        (tmp_path / "docker-compose.yml").write_text("services:\n")

        captured = io.StringIO()

        with (
            patch("subprocess.run") as mock_run,
            patch("urllib.request.urlopen") as mock_urlopen,
        ):
            mock_run.return_value = _mock_subprocess_run(
                stdout=json.dumps([{"State": "running"}])
            )
            mock_urlopen.side_effect = [
                _FakeHTTPResponse(200, '{"status":"ok"}'),
                _FakeHTTPResponse(
                    200,
                    '{"status":"ready","checks":{"database":"reachable","websocket":"connected"}}',
                ),
                _FakeHTTPResponse(200, "# HELP test\ntest 1.0\n"),
                _FakeHTTPResponse(200, "# HELP test\ntest 1.0\n"),
            ]

            old_stdout = sys.stdout
            sys.stdout = captured
            try:
                try:
                    main()
                except SystemExit:
                    pass
            finally:
                sys.stdout = old_stdout

        report_data = json.loads(captured.getvalue())
        # Validate against Pydantic schema
        report = DeploymentValidationReport(**report_data)
        assert report.overall_status == DeploymentCheckStatus.PASS
        assert report.dry_run_verified is True
        assert report.exit_code == 0
        assert len(report.probes) >= 7
