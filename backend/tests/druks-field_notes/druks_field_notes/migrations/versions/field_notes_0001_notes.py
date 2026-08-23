"""field_notes: notes table

Revision ID: field_notes_0001
Revises:
Create Date: 2026-07-07 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# The history root for this app's own alembic_version_field_notes table —
# down_revision is None, and it never links to core's revisions.
revision = "field_notes_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "field_notes_notes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("body", sa.String(), nullable=False),
        sa.Column("gist", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("field_notes_notes")
