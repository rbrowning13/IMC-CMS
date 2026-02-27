"""Add sort_order to claim_surgery

Revision ID: 9b2e818fb54e
Revises: 1bf8d6fd6683
Create Date: 2026-02-26 18:55:23.669313

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9b2e818fb54e'
down_revision: Union[str, Sequence[str], None] = '1bf8d6fd6683'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "claim_surgery",
        sa.Column("sort_order", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("claim_surgery", "sort_order")
