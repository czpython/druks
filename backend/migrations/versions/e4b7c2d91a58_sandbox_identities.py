"""Sandbox identities hold their secrets in rows.

Revision ID: e4b7c2d91a58
Revises: c5e2a7b19d43
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e4b7c2d91a58"
down_revision: str | Sequence[str] | None = "c5e2a7b19d43"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.rename_table("sandbox_grants", "sandbox_identities")
    op.drop_column("sandbox_identities", "services")
    op.create_table(
        "sandbox_secrets",
        sa.Column("identity_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("service", sa.String(), nullable=True),
        sa.Column("subscription_id", sa.String(), nullable=True),
        sa.Column("resource", sa.String(), nullable=False),
        sa.CheckConstraint("(service IS NULL) <> (subscription_id IS NULL)", name="one_source"),
        sa.ForeignKeyConstraint(["identity_id"], ["sandbox_identities.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["service"], ["service_identities.service"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["subscription_id"], ["provider_subscriptions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("identity_id", "name"),
    )


def downgrade() -> None:
    op.drop_table("sandbox_secrets")
    op.add_column(
        "sandbox_identities",
        sa.Column(
            "services",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
    )
    op.rename_table("sandbox_identities", "sandbox_grants")
