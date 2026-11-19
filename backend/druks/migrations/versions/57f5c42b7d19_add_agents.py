"""Add agents.

Revision ID: 57f5c42b7d19
Revises: 1e4a9c2b7d3f
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "57f5c42b7d19"
down_revision: str | Sequence[str] | None = "1e4a9c2b7d3f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agents",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=False),
        sa.Column("instructions", sa.String(), nullable=False),
        sa.Column("skills", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("harness", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("owner_id", sa.String(), nullable=False),
        sa.Column("is_private", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["owner_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("agents_name_lower_key", "agents", [sa.text("lower(name)")], unique=True)


def downgrade() -> None:
    op.drop_table("agents")
