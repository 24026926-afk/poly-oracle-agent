"""add pnl settlement columns to positions

Revision ID: 0003
Revises: 0002
Create Date: 2026-03-30 12:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: Union[str, Sequence[str], None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "positions",
        sa.Column(
            "realized_pnl",
            sa.Numeric(precision=38, scale=18),
            nullable=True,
        ),
    )
    op.add_column(
        "positions",
        sa.Column(
            "exit_price",
            sa.Numeric(precision=38, scale=18),
            nullable=True,
        ),
    )
    op.add_column(
        "positions",
        sa.Column(
            "closed_at_utc",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("positions", "closed_at_utc")
    op.drop_column("positions", "exit_price")
    op.drop_column("positions", "realized_pnl")
