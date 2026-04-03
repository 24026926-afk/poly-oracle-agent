"""add fee columns

Revision ID: 0004
Revises: 0003
Create Date: 2026-04-03 12:14:23.885040

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: Union[str, Sequence[str], None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "positions",
        sa.Column(
            "gas_cost_usdc",
            sa.Numeric(precision=38, scale=18),
            nullable=True,
            comment="Polygon gas cost normalized into USDC at settlement time",
        ),
    )
    op.add_column(
        "positions",
        sa.Column(
            "fees_usdc",
            sa.Numeric(precision=38, scale=18),
            nullable=True,
            comment="Polymarket CLOB maker/taker fees in USDC at settlement time",
        ),
    )


def downgrade() -> None:
    op.drop_column("positions", "fees_usdc")
    op.drop_column("positions", "gas_cost_usdc")
