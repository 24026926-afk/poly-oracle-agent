"""add yes_token_id to market_snapshots

Revision ID: 0005
Revises: 0004
Create Date: 2026-04-04 01:24:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: Union[str, Sequence[str], None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "market_snapshots",
        sa.Column(
            "yes_token_id",
            sa.String(length=256),
            nullable=True,
            comment="CLOB asset ID for the YES outcome (resolved from ws_client mapping)",
        ),
    )


def downgrade() -> None:
    op.drop_column("market_snapshots", "yes_token_id")