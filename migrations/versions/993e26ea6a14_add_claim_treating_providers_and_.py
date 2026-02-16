"""
add claim treating providers and surgeries tables

Revision ID: 993e26ea6a14
Revises: 2235c868c0f8
Create Date: 2026-02-14 08:19:57.422706
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "993e26ea6a14"
down_revision: Union[str, Sequence[str], None] = "2235c868c0f8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""

    # --------------------------------------------------
    # Claim Treating Providers (many-to-many)
    # --------------------------------------------------
    op.create_table(
        "claim_treating_provider",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("claim_id", sa.Integer(), nullable=False),
        sa.Column("provider_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["claim_id"],
            ["claim.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["provider_id"],
            ["provider.id"],
            ondelete="CASCADE",
        ),
    )

    op.create_index(
        "ix_claim_treating_provider_claim_id",
        "claim_treating_provider",
        ["claim_id"],
    )

    op.create_index(
        "ix_claim_treating_provider_provider_id",
        "claim_treating_provider",
        ["provider_id"],
    )

    # --------------------------------------------------
    # Claim Surgeries (one-to-many)
    # --------------------------------------------------
    op.create_table(
        "claim_surgery",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("claim_id", sa.Integer(), nullable=False),
        sa.Column("surgery_date", sa.Date(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["claim_id"],
            ["claim.id"],
            ondelete="CASCADE",
        ),
    )

    op.create_index(
        "ix_claim_surgery_claim_id",
        "claim_surgery",
        ["claim_id"],
    )


def downgrade() -> None:
    """Downgrade schema."""

    op.drop_index("ix_claim_surgery_claim_id", table_name="claim_surgery")
    op.drop_table("claim_surgery")

    op.drop_index("ix_claim_treating_provider_provider_id", table_name="claim_treating_provider")
    op.drop_index("ix_claim_treating_provider_claim_id", table_name="claim_treating_provider")
    op.drop_table("claim_treating_provider")
