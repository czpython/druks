"""Add teams.

Revision ID: 1e4a9c2b7d3f
Revises: f2b6c8d1e4a9
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "1e4a9c2b7d3f"
down_revision: str | Sequence[str] | None = "f2b6c8d1e4a9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "teams",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("apps", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "team_members",
        sa.Column("team_id", sa.String(), nullable=False),
        sa.Column("account_id", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("team_id", "account_id"),
    )


def downgrade() -> None:
    op.drop_table("team_members")
    op.drop_table("teams")
