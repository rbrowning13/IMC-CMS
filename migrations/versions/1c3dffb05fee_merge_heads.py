"""merge heads

Revision ID: 1c3dffb05fee
Revises: 7adb3e8e08b0, fc310f7c3872
Create Date: 2026-01-02 16:30:00

"""

# NOTE: This is a merge revision. It intentionally performs no schema changes.

from alembic import op  # noqa: F401
import sqlalchemy as sa  # noqa: F401


# revision identifiers, used by Alembic.
revision = "1c3dffb05fee"
down_revision = ("7adb3e8e08b0", "fc310f7c3872")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
