"""oauth connections are platform rows

Revision ID: a7c2e9f14b38
Revises: e3a1c8f92d74
Create Date: 2026-08-20 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from druks.core.models import uuid7_str
from druks.secrets import utils

# revision identifiers, used by Alembic.
revision: str = "a7c2e9f14b38"
down_revision: str | Sequence[str] | None = "e3a1c8f92d74"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _reencrypt(envelope: bytes, old_aad: str, new_aad: str) -> bytes:
    # Envelopes bind to their table.column as AAD, so a moved secret must be
    # decrypted under the old identity and sealed under the new one.
    if not envelope:
        return b""
    return utils.encrypt(utils.decrypt(envelope, old_aad), new_aad)


def upgrade() -> None:
    op.create_table(
        "mcp_client_registrations",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("server_id", sa.String(), nullable=False),
        sa.Column("account_id", sa.String(), server_default="system", nullable=False),
        sa.Column("token_endpoint", sa.String(), nullable=False),
        sa.Column("client_id", sa.String(), nullable=False),
        sa.Column("client_secret", sa.LargeBinary(), nullable=False),
        sa.ForeignKeyConstraint(["server_id"], ["mcp_servers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("server_id", "account_id"),
    )
    op.create_table(
        "oauth_connections",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("account_id", sa.String(), nullable=False),
        sa.Column("refresh_token", sa.LargeBinary(), nullable=False),
        sa.Column("scopes", postgresql.JSONB(), nullable=False),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    bind = op.get_bind()
    grants = bind.execute(
        sa.text(
            "SELECT grants.account_id, grants.refresh_token, grants.token_endpoint,"
            " grants.client_id, grants.client_secret, grants.connected_at,"
            " grants.server_name, servers.id AS server_id"
            " FROM mcp_oauth_grants grants"
            " JOIN mcp_servers servers ON servers.name = grants.server_name"
        )
    ).mappings()
    for grant in grants:
        bind.execute(
            sa.text(
                "INSERT INTO mcp_client_registrations"
                " (id, server_id, account_id, token_endpoint, client_id, client_secret)"
                " VALUES (:id, :server_id, :account_id, :token_endpoint, :client_id,"
                " :client_secret)"
            ),
            {
                "id": uuid7_str(),
                "server_id": grant["server_id"],
                "account_id": grant["account_id"],
                "token_endpoint": grant["token_endpoint"],
                "client_id": grant["client_id"],
                "client_secret": _reencrypt(
                    bytes(grant["client_secret"]),
                    "mcp_oauth_grants.client_secret",
                    "mcp_client_registrations.client_secret",
                ),
            },
        )
        bind.execute(
            sa.text(
                "INSERT INTO oauth_connections"
                " (id, provider, account_id, refresh_token, scopes, connected_at)"
                " VALUES (:id, :provider, :account_id, :refresh_token, '[]'::jsonb,"
                " :connected_at)"
            ),
            {
                "id": uuid7_str(),
                "provider": f"mcp:{grant['server_name']}",
                "account_id": grant["account_id"],
                "refresh_token": _reencrypt(
                    bytes(grant["refresh_token"]),
                    "mcp_oauth_grants.refresh_token",
                    "oauth_connections.refresh_token",
                ),
                "connected_at": grant["connected_at"],
            },
        )
    op.drop_table("mcp_oauth_grants")


def downgrade() -> None:
    raise NotImplementedError("oauth rows moved; restore from backup instead")
