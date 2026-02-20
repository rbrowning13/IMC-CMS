"""add initial_next_report_due to claim

Revision ID: 17aba93e77da
Revises: 29ff72b70732
Create Date: 2026-02-19 16:34:12.758836

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '17aba93e77da'
down_revision: Union[str, Sequence[str], None] = '29ff72b70732'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "claim",
        sa.Column("next_report_due", sa.Date(), nullable=True),
    )
    op.drop_column("claim", "initial_next_report_due", if_exists=True)


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column(
        "claim",
        sa.Column("initial_next_report_due", sa.Date(), nullable=True),
    )
    op.drop_column("claim", "next_report_due", if_exists=True)
