from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e6f1a2b3c4d5"
down_revision: str | Sequence[str] | None = "b9e4d21c7a03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            INSERT INTO accounts (id, username, created_at)
            VALUES ('system', 'system', CURRENT_TIMESTAMP)
            ON CONFLICT DO NOTHING
            """
        )
    )
    op.add_column("mcp_servers", sa.Column("identity_mode", sa.String(), nullable=True))
    op.add_column(
        "mcp_oauth_grants",
        sa.Column("account_id", sa.String(), server_default="system", nullable=False),
    )
    op.create_foreign_key(
        "mcp_oauth_grants_account_id_fkey",
        "mcp_oauth_grants",
        "accounts",
        ["account_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.execute(
        sa.text(
            """
            UPDATE mcp_servers
            SET identity_mode = 'shared'
            WHERE EXISTS (
                SELECT 1
                FROM mcp_oauth_grants
                WHERE mcp_oauth_grants.server_name = mcp_servers.name
            )
            """
        )
    )
    op.drop_constraint(
        "mcp_oauth_grants_server_name_key",
        "mcp_oauth_grants",
        type_="unique",
    )
    op.create_unique_constraint(
        "mcp_oauth_grants_server_name_account_id_key",
        "mcp_oauth_grants",
        ["server_name", "account_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "mcp_oauth_grants_server_name_account_id_key",
        "mcp_oauth_grants",
        type_="unique",
    )
    op.create_unique_constraint(
        "mcp_oauth_grants_server_name_key",
        "mcp_oauth_grants",
        ["server_name"],
    )
    op.drop_constraint(
        "mcp_oauth_grants_account_id_fkey",
        "mcp_oauth_grants",
        type_="foreignkey",
    )
    op.drop_column("mcp_oauth_grants", "account_id")
    op.drop_column("mcp_servers", "identity_mode")
