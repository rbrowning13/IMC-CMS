"""merge heads (baseline + claimant)

Revision ID: 1904f6126fa5
Revises: 163eeb7af236, 377d111fd95e
Create Date: 2026-01-29 13:28:52.959102

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1904f6126fa5'
down_revision: Union[str, Sequence[str], None] = ('163eeb7af236', '377d111fd95e')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
