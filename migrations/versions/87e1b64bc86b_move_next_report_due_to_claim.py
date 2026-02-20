"""move next_report_due to claim

Revision ID: 87e1b64bc86b
Revises: 17aba93e77da
Create Date: 2026-02-19 17:20:45.344233

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '87e1b64bc86b'
down_revision: Union[str, Sequence[str], None] = '17aba93e77da'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass

