"""Add the requested scopes to MCP servers.

Revision ID: d4e7a1c90b2f
Revises: cb171bc569c7
Create Date: 2026-10-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d4e7a1c90b2f"
down_revision: str | Sequence[str] | None = "cb171bc569c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("mcp_servers", sa.Column("scopes", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("mcp_servers", "scopes")
