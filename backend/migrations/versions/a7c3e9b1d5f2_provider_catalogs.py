"""Providers own their catalogs; model ids are provider/model.

Revision ID: a7c3e9b1d5f2
Revises: e3a9c7d1b5f4
Create Date: 2026-09-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a7c3e9b1d5f2"
down_revision: str | Sequence[str] | None = "e3a9c7d1b5f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Before this revision only claude and codex stored bare ids; every other
# harness already ran a namespaced model.
_PROVIDER_BY_HARNESS = {"claude": "anthropic", "codex": "openai-codex"}


def upgrade() -> None:
    op.create_table(
        "provider_catalogs",
        sa.Column("provider", sa.String(), primary_key=True),
        sa.Column("models", postgresql.JSONB(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.drop_column("harnesses", "models_fetched")
    op.drop_column("harnesses", "models_fetched_at")
    for harness, provider in _PROVIDER_BY_HARNESS.items():
        op.execute(
            sa.text(
                "UPDATE harnesses SET model = :provider || '/' || model "
                "WHERE name = :harness AND position('/' IN model) = 0"
            ).bindparams(provider=provider, harness=harness)
        )
    # A bare per-agent override was a claude id or a codex id; nothing else
    # ran bare.
    op.execute(
        sa.text(
            "UPDATE settings_overrides SET value = to_jsonb("
            "CASE WHEN value #>> '{}' LIKE 'claude%' THEN 'anthropic/' ELSE 'openai-codex/' END "
            "|| (value #>> '{}')) "
            "WHERE key LIKE 'agent_model:%' AND jsonb_typeof(value) = 'string' "
            "AND position('/' IN value #>> '{}') = 0"
        )
    )


def downgrade() -> None:
    for provider in _PROVIDER_BY_HARNESS.values():
        op.execute(
            sa.text(
                "UPDATE settings_overrides SET value = to_jsonb(substr(value #>> '{}', :cut)) "
                "WHERE key LIKE 'agent_model:%' AND jsonb_typeof(value) = 'string' "
                "AND value #>> '{}' LIKE :prefix"
            ).bindparams(cut=len(provider) + 2, prefix=f"{provider}/%")
        )
    for harness, provider in _PROVIDER_BY_HARNESS.items():
        op.execute(
            sa.text(
                "UPDATE harnesses SET model = substr(model, :cut) "
                "WHERE name = :harness AND model LIKE :prefix"
            ).bindparams(cut=len(provider) + 2, harness=harness, prefix=f"{provider}/%")
        )
    op.add_column(
        "harnesses", sa.Column("models_fetched_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("harnesses", sa.Column("models_fetched", postgresql.JSONB(), nullable=True))
    op.drop_table("provider_catalogs")
