"""fix claim treating providers join table

Revision ID: 3ff94d74a6b5
Revises: b5e51d829ff0
Create Date: 2026-02-15
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "b5e51d829ff0"
down_revision = "bb22f4251c37"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # NOTE: This migration exists because earlier work introduced the claim-owned
    # treating providers join table, but the original migration may not be in the
    # active revision chain for some environments. Make this migration the
    # canonical creator of the join table.

    # Create join table (idempotent-ish: if it already exists, don't crash).
    conn = op.get_bind()
    exists = conn.execute(sa.text("SELECT to_regclass('public.claim_treating_provider')")).scalar()
    if not exists:
        op.create_table(
            "claim_treating_provider",
            sa.Column("claim_id", sa.Integer(), nullable=False),
            sa.Column("provider_id", sa.Integer(), nullable=False),
            sa.Column("sort_order", sa.Integer(), nullable=True),
            sa.ForeignKeyConstraint(["claim_id"], ["claim.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["provider_id"], ["provider.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("claim_id", "provider_id"),
        )

        op.create_index(
            "ix_claim_treating_provider_claim_id",
            "claim_treating_provider",
            ["claim_id"],
            unique=False,
        )
        op.create_index(
            "ix_claim_treating_provider_provider_id",
            "claim_treating_provider",
            ["provider_id"],
            unique=False,
        )

    # Ensure the app role owns and can use the table (safe even if already owned).
    op.execute("ALTER TABLE claim_treating_provider OWNER TO impact")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE claim_treating_provider TO impact")


def downgrade() -> None:
    # Drop indexes/table if present.
    conn = op.get_bind()
    exists = conn.execute(sa.text("SELECT to_regclass('public.claim_treating_provider')")).scalar()
    if exists:
        # Drop indexes first (Postgres will drop dependent indexes with table,
        # but be explicit and resilient if indexes are missing).
        try:
            op.drop_index("ix_claim_treating_provider_provider_id", table_name="claim_treating_provider")
        except Exception:
            pass
        try:
            op.drop_index("ix_claim_treating_provider_claim_id", table_name="claim_treating_provider")
        except Exception:
            pass

        op.drop_table("claim_treating_provider")
