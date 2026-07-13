"""subject keying via workflow attributes

Revision ID: d7e8f9a0b1c2
Revises: c9d0e1f2a3b4
Create Date: 2026-07-13 09:10:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "d7e8f9a0b1c2"
down_revision: str | Sequence[str] | None = "c9d0e1f2a3b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Subject and extension live on the DBOS workflow as custom attributes,
    # stamped at start(); workflow inputs are DBOS's own record.
    op.drop_column("durable_runs", "input")
    op.drop_column("durable_runs", "subject")
    op.drop_column("durable_runs", "extension")


def downgrade() -> None:
    op.add_column("durable_runs", sa.Column("extension", sa.String(), nullable=True))
    op.add_column(
        "durable_runs",
        sa.Column("subject", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "durable_runs",
        sa.Column(
            "input",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
