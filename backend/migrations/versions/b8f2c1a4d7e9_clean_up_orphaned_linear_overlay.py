"""clean up the orphaned Linear overlay

The packaged catalog no longer declares Linear. An install that had enabled the
former built-in kept an overlay row (``set_enabled`` created it with the
static-source default and no token). With the definition gone that row reads as
an enabled, tokenless, static custom server — which fails every run at MCP
delivery with ``MissingTokenError``. Remove exactly that orphan.

Revision ID: b8f2c1a4d7e9
Revises: a4b9d3e17c62
Create Date: 2026-08-26 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b8f2c1a4d7e9"
down_revision: str | Sequence[str] | None = "a4b9d3e17c62"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    # Match the former Linear overlay by its full shape — name ``linear``, static
    # token sourcing, and an empty (never-set) token — not the name alone. A
    # deployment's legitimate custom ``linear`` server carries a real token or a
    # non-static source and is left untouched; a fresh install has no such row
    # and the upgrade is a no-op.
    orphans = (
        bind.execute(
            sa.text(
                "SELECT id, name FROM mcp_servers"
                " WHERE name = 'linear'"
                " AND token_source = 'static'"
                " AND octet_length(token) = 0"
            )
        )
        .mappings()
        .all()
    )
    for orphan in orphans:
        # Move the server's OAuth connections into the same audit state a server
        # removal leaves (see remove_mcp_server / oauth.disconnect): revocation is
        # a state, never a deletion — the consent row survives, stamped revoked
        # with reason ``server_removed`` and its refresh token cleared. Grants are
        # namespaced by provider as ``mcp:<name>`` (see grant_provider).
        bind.execute(
            sa.text(
                "UPDATE oauth_connections"
                " SET revoked_at = COALESCE(revoked_at, now()),"
                " revoked_reason = CASE WHEN revoked_reason = ''"
                " THEN 'server_removed' ELSE revoked_reason END,"
                " refresh_token = ''::bytea"
                " WHERE provider = :provider"
            ),
            {"provider": f"mcp:{orphan['name']}"},
        )
        # Delete the overlay row; its client registrations follow through the FK
        # cascade (mcp_client_registrations.server_id ON DELETE CASCADE).
        bind.execute(sa.text("DELETE FROM mcp_servers WHERE id = :id"), {"id": orphan["id"]})


def downgrade() -> None:
    raise NotImplementedError(
        "the orphaned Linear overlay was deleted and cannot be reconstructed; "
        "restore from backup instead"
    )
