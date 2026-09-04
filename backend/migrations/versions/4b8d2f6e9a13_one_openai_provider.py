"""One OpenAI provider: openai-codex folds into openai as its oauth login.

Revision ID: 4b8d2f6e9a13
Revises: c9d1e3f5a7b2
Create Date: 2026-09-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "4b8d2f6e9a13"
down_revision: str | Sequence[str] | None = "c9d1e3f5a7b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KNOWN = ("anthropic", "openai", "openai-codex")


def upgrade() -> None:
    bind = op.get_bind()
    unknown = bind.scalars(
        sa.text(
            "SELECT DISTINCT provider FROM provider_logins WHERE provider NOT IN :known"
        ).bindparams(sa.bindparam("known", expanding=True, value=list(_KNOWN)))
    ).all()
    if unknown:
        raise RuntimeError(f"login rows name unknown providers {unknown}; disconnect them first")
    # One login per (provider, account): an account holding both rows keeps
    # neither automatically. Name it so the operator disconnects one.
    conflicts = bind.scalars(
        sa.text(
            "SELECT accounts.username FROM provider_logins AS codex "
            "JOIN provider_logins AS platform ON platform.account_id = codex.account_id "
            "AND platform.provider = 'openai' "
            "JOIN accounts ON accounts.id = codex.account_id "
            "WHERE codex.provider = 'openai-codex'"
        )
    ).all()
    if conflicts:
        raise RuntimeError(
            f"accounts {conflicts} hold both an openai-codex and an openai login; "
            "disconnect one first"
        )
    for table in ("provider_logins", "usage_scrapes"):
        op.execute(sa.text(f"UPDATE {table} SET provider = 'openai' WHERE provider = 'openai-codex'"))
    # The subscription catalog is what the codex CLI runs; it wins over the key's.
    op.execute(
        sa.text(
            "DELETE FROM provider_catalogs WHERE provider = 'openai' "
            "AND EXISTS (SELECT 1 FROM provider_catalogs WHERE provider = 'openai-codex')"
        )
    )
    op.execute(sa.text("UPDATE provider_catalogs SET provider = 'openai' WHERE provider = 'openai-codex'"))
    op.execute(
        sa.text(
            "UPDATE user_settings SET default_model = 'openai/' || substr(default_model, 14) "
            "WHERE default_model LIKE 'openai-codex/%'"
        )
    )
    op.execute(
        sa.text(
            "UPDATE settings_overrides SET value = to_jsonb('openai/' || substr(value #>> '{}', 14)) "
            "WHERE key LIKE 'agent_model:%' AND value #>> '{}' LIKE 'openai-codex/%'"
        )
    )


def downgrade() -> None:
    # A subscription login was the openai-codex row; a key login was always openai's.
    op.execute(
        sa.text(
            "UPDATE provider_logins SET provider = 'openai-codex' "
            "WHERE provider = 'openai' AND kind = 'oauth'"
        )
    )
    op.execute(sa.text("UPDATE usage_scrapes SET provider = 'openai-codex' WHERE provider = 'openai'"))
    op.execute(sa.text("UPDATE provider_catalogs SET provider = 'openai-codex' WHERE provider = 'openai'"))
    op.execute(
        sa.text(
            "UPDATE user_settings SET default_model = 'openai-codex/' || substr(default_model, 8) "
            "WHERE default_model LIKE 'openai/%'"
        )
    )
    op.execute(
        sa.text(
            "UPDATE settings_overrides SET value = to_jsonb('openai-codex/' || substr(value #>> '{}', 8)) "
            "WHERE key LIKE 'agent_model:%' AND value #>> '{}' LIKE 'openai/%'"
        )
    )
