"""Add claim-level internal notes

Revision ID: d1a8b0849fa6
Revises: 4b1b8ebfa682
Create Date: 2026-02-27 18:02:18.612211

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd1a8b0849fa6'
down_revision: Union[str, Sequence[str], None] = '4b1b8ebfa682'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('claim', sa.Column('notes', sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('claim', 'notes')
