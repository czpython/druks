"""drop work_items.extension_config_snapshot

Revision ID: b3f1a9c47d20
Revises: 0ec9db44973e
Create Date: 2026-07-19 17:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b3f1a9c47d20"
down_revision: str | Sequence[str] | None = "0ec9db44973e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("work_items", "extension_config_snapshot")


def downgrade() -> None:
    op.add_column(
        "work_items",
        sa.Column(
            "extension_config_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.alter_column("work_items", "extension_config_snapshot", server_default=None)
