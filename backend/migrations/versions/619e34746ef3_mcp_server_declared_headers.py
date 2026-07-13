"""mcp server declared headers

Revision ID: 619e34746ef3
Revises: d7e8f9a0b1c2
Create Date: 2026-07-13 17:57:12.632385

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "619e34746ef3"
down_revision: str | Sequence[str] | None = "d7e8f9a0b1c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Server defaults backfill existing rows — every pre-existing row is a
    # static custom server with no declared headers.
    op.add_column(
        "mcp_servers",
        sa.Column("token_source", sa.String(), nullable=False, server_default="static"),
    )
    op.add_column(
        "mcp_servers",
        sa.Column(
            "headers",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "mcp_servers",
        sa.Column(
            "secret_headers",
            sa.LargeBinary(),
            nullable=False,
            server_default=sa.text("''::bytea"),
        ),
    )


def downgrade() -> None:
    op.drop_column("mcp_servers", "secret_headers")
    op.drop_column("mcp_servers", "headers")
    op.drop_column("mcp_servers", "token_source")
