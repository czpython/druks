"""One operator default model; the model selects the harness.

Revision ID: c9d1e3f5a7b2
Revises: a7c3e9b1d5f2
Create Date: 2026-09-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c9d1e3f5a7b2"
down_revision: str | Sequence[str] | None = "a7c3e9b1d5f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DEFAULT_MODEL = "anthropic/claude-opus-4-7"


def upgrade() -> None:
    op.add_column(
        "user_settings",
        sa.Column("default_model", sa.String(), nullable=False, server_default=_DEFAULT_MODEL),
    )
    # The claude row carried the model most agents declared; it becomes the
    # one default.
    op.execute(
        sa.text(
            "UPDATE user_settings SET default_model = harnesses.model "
            "FROM harnesses WHERE harnesses.name = 'claude'"
        )
    )
    op.drop_column("harnesses", "model")


def downgrade() -> None:
    op.add_column("harnesses", sa.Column("model", sa.String(), nullable=True))
    op.execute(
        sa.text(
            "UPDATE harnesses SET model = COALESCE("
            "(SELECT default_model FROM user_settings LIMIT 1), :fallback)"
        ).bindparams(fallback=_DEFAULT_MODEL)
    )
    op.alter_column("harnesses", "model", nullable=False)
    op.drop_column("user_settings", "default_model")
