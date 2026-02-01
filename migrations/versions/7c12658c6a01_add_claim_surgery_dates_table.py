"""add claim surgery dates table

Revision ID: 7c12658c6a01
Revises: 6109486a47ad
Create Date: 2026-01-30
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "7c12658c6a01"
down_revision = "6109486a47ad"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Multi-surgery table (claim-owned)
    op.create_table(
        "claim_surgery_date",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column(
            "claim_id",
            sa.Integer(),
            sa.ForeignKey("public.claim.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("surgery_date", sa.Date(), nullable=True),
        sa.Column("description", sa.String(255), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=True),
        schema="public",
    )

    op.create_index(
        "ix_claim_surgery_date_claim_id",
        "claim_surgery_date",
        ["claim_id"],
        unique=False,
        schema="public",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_claim_surgery_date_claim_id",
        table_name="claim_surgery_date",
        schema="public",
    )
    op.drop_table("claim_surgery_date", schema="public")
