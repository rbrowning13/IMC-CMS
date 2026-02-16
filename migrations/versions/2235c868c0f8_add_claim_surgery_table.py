"""add claim_surgery table

Revision ID: 2235c868c0f8
Revises: a0d9e44a2abe
Create Date: 2026-02-14 08:19:36.518129

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2235c868c0f8'
down_revision: Union[str, Sequence[str], None] = 'a0d9e44a2abe'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
