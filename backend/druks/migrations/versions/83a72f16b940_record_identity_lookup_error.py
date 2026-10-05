"""Record the safe reason for an identity lookup failure."""

import sqlalchemy as sa
from alembic import op

revision = "83a72f16b940"
down_revision = "9281b05a13da"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("vault", sa.Column("identity_error", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("vault", "identity_error")
