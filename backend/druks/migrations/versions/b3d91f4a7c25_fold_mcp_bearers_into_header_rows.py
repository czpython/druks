"""Fold MCP bearers into header rows.

A pasted MCP bearer and each account's Druks gateway key become the
Authorization header spelled out ("Bearer <token>"), delivered verbatim like
any other secret header. token_source collapses to is_oauth.

Revision ID: b3d91f4a7c25
Revises: 32e4344e7ecf
Create Date: 2026-09-24
"""

import json
from collections.abc import Callable, Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy_encrypted_field import utils

revision: str = "b3d91f4a7c25"
down_revision: str | Sequence[str] | None = "32e4344e7ecf"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BEARER_ROWS = (
    "SELECT id, secrets FROM vault WHERE kind = 'static' AND audience LIKE 'mcp:%' "
    "AND header = 'Authorization' AND revoked_at IS NULL"
)


def _rewrite_values(rewrite: Callable[[str], str]) -> None:
    # The column is ciphertext, so each value is read and written in Python.
    bind = op.get_bind()
    for row in bind.execute(sa.text(_BEARER_ROWS)).mappings():
        secrets = json.loads(utils.decrypt(bytes(row["secrets"]), "vault.secrets"))
        value = rewrite(secrets["value"])
        if value == secrets["value"]:
            continue
        secrets["value"] = value
        plaintext = json.dumps(secrets, separators=(",", ":"), sort_keys=True).encode()
        bind.execute(
            sa.text("UPDATE vault SET secrets = :secrets WHERE id = :id"),
            {"id": row["id"], "secrets": utils.encrypt(plaintext, "vault.secrets")},
        )


def upgrade() -> None:
    _rewrite_values(lambda value: value if value.startswith("Bearer ") else f"Bearer {value}")
    op.add_column(
        "mcp_servers",
        sa.Column("is_oauth", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.execute("UPDATE mcp_servers SET is_oauth = TRUE WHERE token_source = 'oauth'")
    op.drop_column("mcp_servers", "token_source")


def downgrade() -> None:
    _rewrite_values(lambda value: value.removeprefix("Bearer "))
    op.add_column(
        "mcp_servers",
        sa.Column("token_source", sa.String(), nullable=False, server_default="static"),
    )
    op.execute("UPDATE mcp_servers SET token_source = 'oauth' WHERE is_oauth")
    op.drop_column("mcp_servers", "is_oauth")
