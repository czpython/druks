"""Give an agent a picture.

Revision ID: 5e2a9c7d4b18
Revises: 3d8b6f1c9e42
"""

import sqlalchemy as sa
from alembic import op

revision: str = "5e2a9c7d4b18"
down_revision: str | None = "3d8b6f1c9e42"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("agents", sa.Column("picture_url", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("agents", "picture_url")
