"""ensure claim treating provider table exists

Revision ID: 6109486a47ad
Revises: 957ca176e0c1
Create Date: 2026-01-30
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "6109486a47ad"
down_revision = "957ca176e0c1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    # If the table already exists, do nothing.
    if insp.has_table("claim_treating_provider", schema="public"):
        return

    op.create_table(
        "claim_treating_provider",
        sa.Column("claim_id", sa.Integer(), nullable=False),
        sa.Column("provider_id", sa.Integer(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["claim_id"], ["public.claim.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["provider_id"], ["public.provider.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("claim_id", "provider_id"),
        schema="public",
    )

    # helpful indexes
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_claim_treating_provider_claim_id "
        "ON public.claim_treating_provider (claim_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_claim_treating_provider_provider_id "
        "ON public.claim_treating_provider (provider_id)"
    )


def downgrade() -> None:
    # NO-OP on purpose: never drop this table on a downgrade (production safety).
    pass