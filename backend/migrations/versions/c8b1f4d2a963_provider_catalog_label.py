"""A provider catalog names its provider.

Revision ID: c8b1f4d2a963
Revises: 7e1c4b9d2a58
Create Date: 2026-09-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c8b1f4d2a963"
down_revision: str | Sequence[str] | None = "7e1c4b9d2a58"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("provider_catalogs", sa.Column("label", sa.String(), nullable=True))
    # Only registered providers have rows so far; the next refresh rewrites these.
    op.execute(
        sa.text(
            "UPDATE provider_catalogs SET label = CASE provider "
            "WHEN 'anthropic' THEN 'Anthropic' WHEN 'openai' THEN 'OpenAI' "
            "ELSE initcap(provider) END"
        )
    )
    op.alter_column("provider_catalogs", "label", nullable=False)


def downgrade() -> None:
    op.drop_column("provider_catalogs", "label")
