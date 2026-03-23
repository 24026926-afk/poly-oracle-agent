#!/usr/bin/env python3
"""Initialize the database schema via Alembic migrations."""

from __future__ import annotations

import sys
from pathlib import Path

import structlog
from alembic import command
from alembic.config import Config

logger = structlog.get_logger(__name__)


def _build_alembic_config() -> Config:
    project_root = Path(__file__).resolve().parents[1]
    alembic_ini_path = project_root / "alembic.ini"

    cfg = Config(str(alembic_ini_path))
    cfg.set_main_option("script_location", str(project_root / "migrations"))
    return cfg


def init_db() -> None:
    """Apply all migrations up to ``head``."""
    command.upgrade(_build_alembic_config(), "head")
    logger.info("database_initialized", migration_target="head")


if __name__ == "__main__":
    try:
        init_db()
    except Exception as exc:
        logger.exception("database_init_failed", error=str(exc))
        sys.exit(1)
