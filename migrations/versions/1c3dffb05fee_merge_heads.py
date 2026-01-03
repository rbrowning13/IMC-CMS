"""merge heads

Revision ID: 1c3dffb05fee
Revises: 377d111fd95e, 7adb3e8e08b0
Create Date: 2026-01-02 16:30:04.662129

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1c3dffb05fee'
down_revision: Union[str, Sequence[str], None] = ('377d111fd95e', '7adb3e8e08b0')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
