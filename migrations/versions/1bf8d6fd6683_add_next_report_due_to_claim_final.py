"""add next_report_due to claim final

Revision ID: 1bf8d6fd6683
Revises: 87e1b64bc86b
Create Date: 2026-02-19 17:43:00.210923

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1bf8d6fd6683'
down_revision: Union[str, Sequence[str], None] = '87e1b64bc86b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Add next_report_due column to claim
    op.add_column(
        "claim",
        sa.Column("next_report_due", sa.Date(), nullable=True),
    )

    # Migrate existing data from initial_next_report_due
    op.execute(
        """
        UPDATE claim
        SET next_report_due = initial_next_report_due
        WHERE initial_next_report_due IS NOT NULL
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("claim", "next_report_due")
