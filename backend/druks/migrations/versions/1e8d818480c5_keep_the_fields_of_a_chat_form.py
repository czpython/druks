"""A notification keeps the fields of a form that a chat's agent asks, so Druks checks the
answers of a Submit against them after the turn's record of the question is gone."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "1e8d818480c5"
down_revision = "5f4cbe1efb1b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("notifications", sa.Column("fields", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("notifications", "fields")
