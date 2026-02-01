"""fix claim treating providers join table

Revision ID: 3ff94d74a6b5
Revises: ec9f5c7c243e
Create Date: 2026-01-29
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "3ff94d74a6b5"
down_revision = "ec9f5c7c243e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Ensure the claim_treating_provider join table exists.

    IMPORTANT:
    - This is written to be *idempotent*.
    - It uses schema-qualified names (public.*) to avoid search_path surprises.
    - Prefer Alembic DDL helpers over raw SQL to avoid driver quirks.
    """

    bind = op.get_bind()
    insp = sa.inspect(bind)

    # If the table already exists, do nothing.
    if insp.has_table("claim_treating_provider", schema="public"):
        return

    # Create the join table (schema-qualified).
    op.create_table(
        "claim_treating_provider",
        sa.Column(
            "claim_id",
            sa.Integer(),
            sa.ForeignKey("public.claim.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "provider_id",
            sa.Integer(),
            sa.ForeignKey("public.provider.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        schema="public",
    )

    # Helpful indexes (use IF NOT EXISTS for safety across drifted environments).
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_claim_treating_provider_claim_id
            ON public.claim_treating_provider (claim_id)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_claim_treating_provider_provider_id
            ON public.claim_treating_provider (provider_id)
        """
    )


def downgrade() -> None:
    """Do NOT drop the table on downgrade.

    This table may contain production data and was missing due to drift.
    """

    # Intentionally no-op
    pass
