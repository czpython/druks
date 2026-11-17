"""A subscription remembers when its account was warned that its sign-in ends, so the
warning goes out once for each sign-in."""

import sqlalchemy as sa
from alembic import op

revision = "b9d643a10663"
down_revision = "a7c2e94d1b05"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "vault",
        sa.Column("sign_in_end_warned_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("vault", "sign_in_end_warned_at")
