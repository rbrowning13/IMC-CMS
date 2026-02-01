"""add claim surgery dates table

Revision ID: 36f30e4e8068
Revises: b5e51d829ff0
Create Date: 2026-01-29 12:27:30.635279

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '36f30e4e8068'
down_revision: Union[str, Sequence[str], None] = 'b5e51d829ff0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "claim_surgery",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("claim_id", sa.Integer(), nullable=False),
        sa.Column("surgery_date", sa.Date(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["claim_id"], ["claim.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "ix_claim_surgery_claim_id",
        "claim_surgery",
        ["claim_id"],
        unique=False,
    )
    op.create_index(
        "ix_claim_surgery_surgery_date",
        "claim_surgery",
        ["surgery_date"],
        unique=False,
    )

    # Backfill from legacy single-column claim.surgery_date if present
    conn = op.get_bind()
    insp = sa.inspect(conn)

    try:
        cols = [c.get("name") for c in insp.get_columns("claim")]
    except Exception:
        cols = []

    if cols and "surgery_date" in {str(c).lower() for c in cols if c}:
        conn.execute(
            sa.text(
                """
                INSERT INTO claim_surgery (claim_id, surgery_date, description, sort_order, created_at)
                SELECT id, surgery_date, NULL, 1, NOW()
                FROM claim
                WHERE surgery_date IS NOT NULL
                """
            )
        )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_claim_surgery_surgery_date", table_name="claim_surgery")
    op.drop_index("ix_claim_surgery_claim_id", table_name="claim_surgery")
    op.drop_table("claim_surgery")
