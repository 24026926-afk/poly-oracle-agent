"""Integration smoke test for Alembic migrations."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EXPECTED_TABLES = {
    "market_snapshots",
    "agent_decision_logs",
    "execution_txs",
}


def _build_config(database_url: str) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


def _list_tables(sync_database_url: str) -> set[str]:
    engine = create_engine(sync_database_url)
    try:
        with engine.connect() as conn:
            inspector = inspect(conn)
            return set(inspector.get_table_names())
    finally:
        engine.dispose()


def test_migration_upgrade_downgrade_reupgrade_smoke(tmp_path) -> None:
    db_file = tmp_path / "migration_smoke.db"
    async_url = f"sqlite+aiosqlite:///{db_file}"
    sync_url = f"sqlite:///{db_file}"
    cfg = _build_config(async_url)

    command.upgrade(cfg, "head")
    upgraded_tables = _list_tables(sync_url)
    assert EXPECTED_TABLES.issubset(upgraded_tables)

    command.downgrade(cfg, "base")
    downgraded_tables = _list_tables(sync_url)
    assert EXPECTED_TABLES.isdisjoint(downgraded_tables)

    command.upgrade(cfg, "head")
    reupgraded_tables = _list_tables(sync_url)
    assert EXPECTED_TABLES.issubset(reupgraded_tables)
