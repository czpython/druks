"""require harness provider email

Revision ID: a4c8e2f7b913
Revises: d9f3a7c1e5b2
Create Date: 2026-08-02 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "a4c8e2f7b913"
down_revision: str | Sequence[str] | None = "d9f3a7c1e5b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(sa.text("DELETE FROM harness_logins WHERE provider_email IS NULL"))
    op.alter_column(
        "harness_logins",
        "provider_email",
        existing_type=postgresql.CITEXT(),
        nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "harness_logins",
        "provider_email",
        existing_type=postgresql.CITEXT(),
        nullable=True,
    )
