#!/usr/bin/env python3
"""
scripts/ops/periodic_runtime_audit.py

WI-61 — Periodic Runtime Audit CLI entrypoint.

Runs the deterministic, read-only runtime auditor and exits with typed
codes: 0=healthy, 1=degraded, 2=safety-gate failure, 3=probe error.

Usage:
    python scripts/ops/periodic_runtime_audit.py [OPTIONS]

Options:
    --base-url URL        Health server base URL (default: http://127.0.0.1:8080)
    --output-dir PATH     Artifact output directory (default: docs/operations/runtime_audits)
    --db-path PATH        SQLite database file path (default: poly_oracle.db)
    --http-timeout SEC    HTTP probe timeout in seconds (default: 10)
    --telegram-enabled    Enable Telegram alerts (default: disabled)
    --skip-docker         Skip Docker probe (default: skip)
    --skip-log-tail       Skip log tail probe (default: skip)
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.observability.runtime_audit import run_audit
from src.schemas.runtime_audit import RuntimeAuditExitCode

logger = structlog.get_logger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    """CLI entrypoint. Returns typed exit code."""
    parser = argparse.ArgumentParser(
        description="Periodic Runtime Audit — WI-61",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8080",
        help="Health server base URL (default: http://127.0.0.1:8080)",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Artifact output directory (default: docs/operations/runtime_audits)",
    )
    parser.add_argument(
        "--db-path",
        default=None,
        help="SQLite database file path (default: poly_oracle.db)",
    )
    parser.add_argument(
        "--http-timeout",
        type=float,
        default=10.0,
        help="HTTP probe timeout in seconds (default: 10)",
    )
    parser.add_argument(
        "--telegram-enabled",
        action="store_true",
        default=False,
        help="Enable Telegram alerts (default: disabled)",
    )
    parser.add_argument(
        "--skip-docker",
        action="store_true",
        default=True,
        help="Skip Docker probe (default: skip)",
    )
    parser.add_argument(
        "--skip-log-tail",
        action="store_true",
        default=True,
        help="Skip log tail probe (default: skip)",
    )

    args = parser.parse_args()

    output_dir = None
    if args.output_dir:
        output_dir = Path(args.output_dir)
        try:
            output_dir.resolve().relative_to(_PROJECT_ROOT.resolve())
        except ValueError:
            logger.error(
                "cli.output_dir_outside_project",
                output_dir=str(output_dir),
                project_root=str(_PROJECT_ROOT),
            )
            return RuntimeAuditExitCode.PROBE_ERROR.value

    async def _run() -> int:
        # Create read-only session factory for repository summaries
        db_path = args.db_path or "poly_oracle.db"
        # Use SQLite URI mode to enforce read-only access
        db_url = f"sqlite+aiosqlite:///file:{db_path}?mode=ro&uri=true"
        engine = create_async_engine(db_url, echo=False)
        session_factory = async_sessionmaker(
            bind=engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        
        async with httpx.AsyncClient() as client:
            report = await run_audit(
                http_client=client,
                base_url=args.base_url,
                db_path=args.db_path,
                telegram_enabled=args.telegram_enabled,
                output_dir=output_dir,
                project_root=_PROJECT_ROOT,
                http_timeout=args.http_timeout,
                skip_docker=args.skip_docker,
                skip_log_tail=args.skip_log_tail,
                session_factory=session_factory,
            )
        
        await engine.dispose()
        
        logger.info(
            "audit.complete",
            status=report.status.value,
            exit_code=report.exit_code.value,
            finding_count=len(report.findings),
        )
        return report.exit_code.value

    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        logger.warning("audit.interrupted")
        return RuntimeAuditExitCode.PROBE_ERROR.value
    except Exception as exc:
        logger.error("audit.fatal_error", error=str(exc), exc_type=type(exc).__name__)
        return RuntimeAuditExitCode.PROBE_ERROR.value


if __name__ == "__main__":
    sys.exit(main())
