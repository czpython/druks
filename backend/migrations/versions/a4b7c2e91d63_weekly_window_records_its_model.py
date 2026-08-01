from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a4b7c2e91d63"
down_revision: str | Sequence[str] | None = "f7c1a0e9d2b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("usage_scrapes", sa.Column("week_model", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("usage_scrapes", "week_model")
