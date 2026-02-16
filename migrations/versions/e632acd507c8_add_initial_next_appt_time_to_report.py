"""Add initial_next_appt_time to Report

Revision ID: e632acd507c8
Revises: 1fbe32218d4f
Create Date: 2026-02-15 09:55:18.733424

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e632acd507c8'
down_revision: Union[str, Sequence[str], None] = '1fbe32218d4f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema: add initial_next_appt_time column to Report table."""
    op.add_column(
        'report',
        sa.Column('initial_next_appt_time', sa.Time(), nullable=True)
    )


def downgrade() -> None:
    """Downgrade schema: remove initial_next_appt_time column from Report table."""
    op.drop_column('report', 'initial_next_appt_time')
