"""add open positions table

Revision ID: 0002
Revises: 0001
Create Date: 2026-03-29 18:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: Union[str, Sequence[str], None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "positions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("condition_id", sa.String(length=256), nullable=False),
        sa.Column("token_id", sa.String(length=256), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("entry_price", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column(
            "order_size_usdc", sa.Numeric(precision=38, scale=18), nullable=False
        ),
        sa.Column("kelly_fraction", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column(
            "best_ask_at_entry", sa.Numeric(precision=38, scale=18), nullable=False
        ),
        sa.Column(
            "bankroll_usdc_at_entry",
            sa.Numeric(precision=38, scale=18),
            nullable=False,
        ),
        sa.Column("execution_action", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=512), nullable=True),
        sa.Column("routed_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_positions_condition_id", "positions", ["condition_id"], unique=False
    )
    op.create_index("ix_positions_status", "positions", ["status"], unique=False)
    op.create_index(
        "ix_positions_condition_id_status",
        "positions",
        ["condition_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_positions_condition_id_status", table_name="positions")
    op.drop_index("ix_positions_status", table_name="positions")
    op.drop_index("ix_positions_condition_id", table_name="positions")
    op.drop_table("positions")
