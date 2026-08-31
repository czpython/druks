"""Name a file's source.

Revision ID: b4c7e1a8d052
Revises: c1e8b4a9f2d7
Create Date: 2026-08-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b4c7e1a8d052"
down_revision: str | Sequence[str] | None = "c1e8b4a9f2d7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("files", sa.Column("uploaded_by", sa.String(), nullable=True))
    op.add_column("files", sa.Column("agent_call_id", sa.String(), nullable=True))
    # origin_id carried no foreign key, so some rows name a source that is gone.
    # Those keep the source they already had: none.
    op.execute(
        """
        UPDATE files SET agent_call_id = origin_id
        WHERE origin_type = 'agent_call'
          AND origin_id IN (SELECT id FROM agent_calls)
        """
    )
    op.execute(
        """
        UPDATE files SET uploaded_by = origin_id
        WHERE origin_type = 'user_upload'
          AND origin_id IN (SELECT id FROM accounts)
        """
    )
    op.create_foreign_key(
        "files_uploaded_by_fkey", "files", "accounts", ["uploaded_by"], ["id"], ondelete="RESTRICT"
    )
    op.create_foreign_key(
        "files_agent_call_id_fkey",
        "files",
        "agent_calls",
        ["agent_call_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.drop_constraint("files_origin_type_check", "files")
    op.create_check_constraint(
        "files_one_source_check", "files", "num_nonnulls(uploaded_by, agent_call_id) <= 1"
    )
    op.drop_column("files", "origin_type")
    op.drop_column("files", "origin_id")


def downgrade() -> None:
    op.add_column("files", sa.Column("origin_type", sa.String(), nullable=True))
    op.add_column("files", sa.Column("origin_id", sa.String(), nullable=True))
    op.execute(
        """
        UPDATE files SET
            origin_type = CASE WHEN agent_call_id IS NULL THEN 'user_upload' ELSE 'agent_call' END,
            origin_id = coalesce(agent_call_id, uploaded_by, '')
        """
    )
    op.alter_column("files", "origin_type", nullable=False)
    op.alter_column("files", "origin_id", nullable=False)
    op.drop_constraint("files_one_source_check", "files")
    op.create_check_constraint(
        "files_origin_type_check", "files", "origin_type IN ('agent_call', 'user_upload')"
    )
    op.drop_constraint("files_agent_call_id_fkey", "files", type_="foreignkey")
    op.drop_constraint("files_uploaded_by_fkey", "files", type_="foreignkey")
    op.drop_column("files", "agent_call_id")
    op.drop_column("files", "uploaded_by")
