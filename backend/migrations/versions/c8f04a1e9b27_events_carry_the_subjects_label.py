"""events carry the subject's label

Revision ID: c8f04a1e9b27
Revises: b5e91c2d7a34
Create Date: 2026-07-27 00:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c8f04a1e9b27"
down_revision: str | Sequence[str] | None = "b5e91c2d7a34"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("events", sa.Column("subject_label", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("events", "subject_label")
