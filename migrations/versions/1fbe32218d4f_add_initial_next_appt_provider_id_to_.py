"""Add initial_next_appt_provider_id to Report

Revision ID: 1fbe32218d4f
Revises: 993e26ea6a14
Create Date: 2026-02-14 09:16:23.829440

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1fbe32218d4f'
down_revision: Union[str, Sequence[str], None] = '993e26ea6a14'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "report",
        sa.Column("initial_next_appt_provider_id", sa.Integer(), nullable=True)
    )
    op.create_foreign_key(
        None,
        "report",
        "provider",
        ["initial_next_appt_provider_id"],
        ["id"]
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(None, "report", type_="foreignkey")
    op.drop_column("report", "initial_next_appt_provider_id")
