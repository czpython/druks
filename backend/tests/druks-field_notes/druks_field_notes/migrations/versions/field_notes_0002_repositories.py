"""field_notes: repositories table

Revision ID: field_notes_0002
Revises: field_notes_0001
Create Date: 2026-09-06 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "field_notes_0002"
down_revision = "field_notes_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "field_notes_repositories",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("repo", sa.String(), nullable=False),
        sa.Column("gist", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("repo"),
    )


def downgrade() -> None:
    op.drop_table("field_notes_repositories")
