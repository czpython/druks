"""Allow a two-letter prefix plus a digit 1-9.

Revision ID: a3c9e1f4b072
Revises: f2b8d5c0e394
Create Date: 2026-09-10
"""

from collections.abc import Sequence

from alembic import op

revision: str = "a3c9e1f4b072"
down_revision: str | Sequence[str] | None = "f2b8d5c0e394"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PREFIX_SHAPE = "(prefix IS NULL) OR (prefix ~ '^([A-Z]{2,6}|[A-Z]{2}[1-9])$')"


def upgrade() -> None:
    op.drop_constraint("projects_prefix_shape", "projects", type_="check")
    op.create_check_constraint("projects_prefix_shape", "projects", _PREFIX_SHAPE)


def downgrade() -> None:
    raise NotImplementedError("Digit prefixes stay legal.")
