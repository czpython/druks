"""Store the provider's main usage-limit status.

Revision ID: 6e9c2b4a8f10
Revises: d3a7f1c9e5b2
"""

import sqlalchemy as sa
from alembic import op

revision = "6e9c2b4a8f10"
down_revision = "d3a7f1c9e5b2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("usage_scrapes", sa.Column("main_limit_reached", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("usage_scrapes", "main_limit_reached")
