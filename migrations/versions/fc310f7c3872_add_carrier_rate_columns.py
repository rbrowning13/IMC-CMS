"""add carrier rate columns

Revision ID: fc310f7c3872
Revises: 
Create Date: 2025-12-25 08:36:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "fc310f7c3872"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Carrier-specific rate overrides. Nullable => fall back to global Settings rates.
    op.add_column("carrier", sa.Column("hourly_rate", sa.Numeric(10, 2), nullable=True))
    op.add_column("carrier", sa.Column("telephonic_rate", sa.Numeric(10, 2), nullable=True))
    # Mileage sometimes needs more precision.
    op.add_column("carrier", sa.Column("mileage_rate", sa.Numeric(10, 4), nullable=True))


def downgrade() -> None:
    op.drop_column("carrier", "mileage_rate")
    op.drop_column("carrier", "telephonic_rate")
    op.drop_column("carrier", "hourly_rate")