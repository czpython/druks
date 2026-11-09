"""Record when a subscription's refresh token ends."""

import sqlalchemy as sa
from alembic import op

revision = "5cf17f5e02c3"
down_revision = "c4f1a8e27d39"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "vault", sa.Column("refresh_token_expires_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("vault", "refresh_token_expires_at")
