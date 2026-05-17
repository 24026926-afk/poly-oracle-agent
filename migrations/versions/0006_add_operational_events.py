"""add operational_events table for WI-56

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-14 10:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: Union[str, Sequence[str], None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "operational_events",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column("payload_json", sa.Text, nullable=False),
        sa.Column("persistence_status", sa.String(length=16), nullable=False),
        sa.Column("created_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=False,
    )
    op.create_index(
        "ix_operational_events_created_at",
        "operational_events",
        ["created_at_utc"],
    )
    op.create_index(
        "ix_operational_events_event_type",
        "operational_events",
        ["event_type"],
    )
    op.create_index(
        "ix_operational_events_severity_composite",
        "operational_events",
        ["severity", "created_at_utc"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_operational_events_severity_composite", table_name="operational_events"
    )
    op.drop_index("ix_operational_events_event_type", table_name="operational_events")
    op.drop_index("ix_operational_events_created_at", table_name="operational_events")
    op.drop_table("operational_events")
