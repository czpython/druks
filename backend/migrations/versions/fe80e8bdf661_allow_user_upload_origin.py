"""Allow user_upload origin.

Revision ID: fe80e8bdf661
Revises: e7b2c9d4f601
Create Date: 2026-08-27
"""

from collections.abc import Sequence

from alembic import op

revision: str = "fe80e8bdf661"
down_revision: str | Sequence[str] | None = "e7b2c9d4f601"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("files_origin_type_check", "files")
    op.create_check_constraint(
        "files_origin_type_check",
        "files",
        "origin_type IN ('agent_call', 'user_upload')",
    )


def downgrade() -> None:
    op.drop_constraint("files_origin_type_check", "files")
    op.create_check_constraint(
        "files_origin_type_check",
        "files",
        "origin_type IN ('agent_call')",
    )
