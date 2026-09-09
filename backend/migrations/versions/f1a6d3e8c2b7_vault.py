"""The vault keeps every secret.

Revision ID: f1a6d3e8c2b7
Revises: e4b7c2d91a58
"""

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy_encrypted_field import utils
from uuid_utils import uuid7

revision: str = "f1a6d3e8c2b7"
down_revision: str | Sequence[str] | None = "e4b7c2d91a58"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "vault",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("account_id", sa.String(), nullable=True),
        sa.Column("audience", sa.String(), nullable=False),
        sa.Column("header", sa.String(), nullable=False),
        sa.Column("secrets", sa.LargeBinary(), nullable=False),
        sa.Column("identity", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("scopes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_vault_one_per_audience",
        "vault",
        ["kind", "audience", "account_id", "header"],
        unique=True,
        postgresql_where=sa.text("kind <> 'oauth'"),
        postgresql_nulls_not_distinct=True,
    )
    _move_provider_keys()
    _move_service_identities()
    _move_oauth_grants()
    _move_mcp_secrets()
    _move_subscriptions()
    _move_sandbox_refs()
    op.drop_table("service_identities")
    op.drop_table("provider_subscriptions")


def _row(
    *,
    kind: str,
    secret_id: str | None = None,
    audience: str,
    secrets: dict[str, Any],
    account_id: str | None = None,
    header: str = "",
    identity: dict[str, Any] | None = None,
    scopes: list[str] | None = None,
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
    expires_at: datetime | None = None,
    revoked_at: datetime | None = None,
    revoked_reason: str = "",
) -> str:
    """Insert one vault row, its secrets encrypted under the vault's own
    binding, and return its id."""
    now = datetime.now(UTC)
    secret_id = secret_id or str(uuid7())
    plaintext = json.dumps(secrets, separators=(",", ":"), sort_keys=True).encode()
    op.get_bind().execute(
        sa.text(
            "INSERT INTO vault (id, kind, account_id, audience, header, secrets, identity, scopes, "
            "created_at, updated_at, expires_at, revoked_at, revoked_reason) VALUES "
            "(:id, :kind, :account_id, :audience, :header, :secrets, :identity, :scopes, "
            ":created_at, :updated_at, :expires_at, :revoked_at, :revoked_reason)"
        ),
        {
            "id": secret_id,
            "kind": kind,
            "account_id": account_id,
            "audience": audience,
            "header": header,
            "secrets": utils.encrypt(plaintext, "vault.secrets"),
            "identity": json.dumps(identity or {}),
            "scopes": json.dumps(scopes or []),
            "created_at": created_at or now,
            "updated_at": updated_at or created_at or now,
            "expires_at": expires_at,
            "revoked_at": revoked_at,
            "revoked_reason": revoked_reason,
        },
    )
    return secret_id


def _read(query: str) -> list[dict[str, Any]]:
    return [dict(row) for row in op.get_bind().execute(sa.text(query)).mappings()]


def _drop_foreign_key(table: str, column: str) -> None:
    for key in sa.inspect(op.get_bind()).get_foreign_keys(table):
        if key["constrained_columns"] == [column]:
            op.drop_constraint(key["name"], table, type_="foreignkey")


def _move_provider_keys() -> None:
    # A pasted key is a static row, the paster a fact on it. A disconnected
    # key is a revoked row, so the calls that named it keep their reference.
    ids: dict[str, str] = {}
    for key in _read(
        "SELECT provider, value, disconnected_at, updated_by_account_id, updated_at "
        "FROM provider_keys"
    ):
        value = bytes(key["value"]) if key["value"] else b""
        secrets = (
            {"value": utils.decrypt(value, "provider_keys.value").decode()}
            if value and not key["disconnected_at"]
            else {}
        )
        ids[key["provider"]] = _row(
            kind="static",
            audience=f"provider:{key['provider']}",
            secrets=secrets,
            identity={"pasted_by": key["updated_by_account_id"]},
            created_at=key["updated_at"],
            revoked_at=key["disconnected_at"],
            revoked_reason="user" if key["disconnected_at"] else "",
        )
    op.add_column("agent_calls", sa.Column("api_key_id", sa.String(), nullable=True))
    for provider, secret_id in ids.items():
        op.get_bind().execute(
            sa.text("UPDATE agent_calls SET api_key_id = :id WHERE api_key_provider = :provider"),
            {"id": secret_id, "provider": provider},
        )
    op.drop_constraint("agent_calls_billing_source_check", "agent_calls", type_="check")
    _drop_foreign_key("agent_calls", "api_key_provider")
    op.drop_column("agent_calls", "api_key_provider")
    op.create_foreign_key(
        "agent_calls_api_key_id_fkey", "agent_calls", "vault", ["api_key_id"], ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "agent_calls_billing_source_check",
        "agent_calls",
        "(subscription_id IS NOT NULL) <> (api_key_id IS NOT NULL)",
        postgresql_not_valid=True,
    )
    op.drop_table("provider_keys")


def _move_service_identities() -> None:
    # The appliance's identity at a service. A private key makes it an App
    # key that issues tokens; anything else is pasted values.
    for row in _read("SELECT service, identity, secrets, connected_at FROM service_identities"):
        secrets = json.loads(utils.decrypt(bytes(row["secrets"]), "service_identities.secrets"))
        _row(
            kind="app_key" if "private_key" in secrets else "static",
            audience=f"service:{row['service']}",
            secrets=secrets,
            identity=row["identity"],
            created_at=row["connected_at"],
        )


def _move_oauth_grants() -> None:
    # A grant and the client it refreshes through become one row: the refresh
    # token and, for an MCP server, the registered client in its secrets.
    clients: dict[tuple[str, str | None], dict[str, str]] = {}
    for row in _read(
        "SELECT s.name, r.account_id, r.token_endpoint, r.client_id, r.client_secret "
        "FROM mcp_client_registrations r JOIN mcp_servers s ON s.id = r.server_id"
    ):
        secret = bytes(row["client_secret"]) if row["client_secret"] else b""
        clients[(row["name"], row["account_id"])] = {
            "token_endpoint": row["token_endpoint"],
            "client_id": row["client_id"],
            "client_secret": (
                utils.decrypt(secret, "mcp_client_registrations.client_secret").decode()
                if secret
                else ""
            ),
        }
    for row in _read(
        "SELECT id, provider, account_id, refresh_token, scopes, identity, connected_at, "
        "revoked_at, revoked_reason FROM oauth_connections"
    ):
        provider = row["provider"]
        audience = provider if provider.startswith("mcp:") else f"service:{provider}"
        envelope = bytes(row["refresh_token"]) if row["refresh_token"] else b""
        secrets: dict[str, Any] = {}
        if envelope and not row["revoked_at"]:
            secrets["refresh_token"] = utils.decrypt(
                envelope, "oauth_connections.refresh_token"
            ).decode()
            secrets.update(clients.get((provider.partition(":")[2], row["account_id"]), {}))
        _row(
            kind="oauth",
            secret_id=row["id"],
            audience=audience,
            secrets=secrets,
            account_id=row["account_id"],
            identity=row["identity"],
            scopes=row["scopes"],
            created_at=row["connected_at"],
            revoked_at=row["revoked_at"],
            revoked_reason=row["revoked_reason"] or "",
        )
    op.drop_table("mcp_client_registrations")
    op.drop_table("oauth_connections")


def _move_mcp_secrets() -> None:
    # A server's bearer and each of its secret headers become one static row
    # each, under the header the value fills.
    for row in _read("SELECT name, token, secret_headers, created_at FROM mcp_servers"):
        audience = f"mcp:{row['name']}"
        token = bytes(row["token"]) if row["token"] else b""
        if token:
            _row(
                kind="static",
                audience=audience,
                header="Authorization",
                secrets={"value": utils.decrypt(token, "mcp_servers.token").decode()},
                created_at=row["created_at"],
            )
        envelope = bytes(row["secret_headers"]) if row["secret_headers"] else b""
        headers = (
            json.loads(utils.decrypt(envelope, "mcp_servers.secret_headers")) if envelope else {}
        )
        for header, value in headers.items():
            _row(
                kind="static",
                audience=audience,
                header=header,
                secrets={"value": value},
                created_at=row["created_at"],
            )
    op.drop_column("mcp_servers", "token")
    op.drop_column("mcp_servers", "secret_headers")


def _move_subscriptions() -> None:
    # A login keeps its id, so the calls billed to it and the boxes that
    # fetch it point at the vault row unchanged.
    for row in _read(
        "SELECT id, provider, account_id, provider_email, payload, expires_at, "
        "disconnected_at, updated_at FROM provider_subscriptions"
    ):
        payload = bytes(row["payload"]) if row["payload"] else b""
        secrets = (
            json.loads(utils.decrypt(payload, "provider_subscriptions.payload"))
            if payload and not row["disconnected_at"]
            else {}
        )
        _row(
            secret_id=row["id"],
            kind="subscription",
            audience=f"provider:{row['provider']}",
            secrets=secrets,
            account_id=row["account_id"],
            identity={"email": row["provider_email"]},
            created_at=row["updated_at"],
            expires_at=row["expires_at"],
            revoked_at=row["disconnected_at"],
            revoked_reason="user" if row["disconnected_at"] else "",
        )
    op.drop_constraint("agent_calls_subscription_id_fkey", "agent_calls", type_="foreignkey")
    op.create_foreign_key(
        "agent_calls_subscription_id_fkey",
        "agent_calls",
        "vault",
        ["subscription_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def _move_sandbox_refs() -> None:
    # A box's rows point at vault rows: a login by its kept id, a service
    # identity by the row that took its slug.
    op.create_table(
        "sandbox_secret_refs",
        sa.Column("identity_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("secret_id", sa.String(), nullable=False),
        sa.Column("resource", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["identity_id"], ["sandbox_identities.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["secret_id"], ["vault.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("identity_id", "name"),
    )
    op.get_bind().execute(
        sa.text(
            "INSERT INTO sandbox_secret_refs (identity_id, name, secret_id, resource) "
            "SELECT s.identity_id, s.name, COALESCE(s.subscription_id, v.id), s.resource "
            "FROM sandbox_secrets s LEFT JOIN vault v "
            "ON v.kind IN ('app_key', 'static') AND v.audience = 'service:' || s.service"
        )
    )
    op.drop_table("sandbox_secrets")


def downgrade() -> None:
    _restore_mcp_secrets()
    _restore_oauth_grants()
    _restore_subscriptions()
    _restore_service_identities()
    _restore_provider_keys()
    _restore_sandbox_secrets()
    op.drop_index("ix_vault_one_per_audience", table_name="vault")
    op.drop_table("vault")


def _restore_oauth_grants() -> None:
    op.create_table(
        "oauth_connections",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("account_id", sa.String(), nullable=True),
        sa.Column("refresh_token", sa.LargeBinary(), nullable=False),
        sa.Column("scopes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("identity", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "mcp_client_registrations",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("server_id", sa.String(), nullable=False),
        sa.Column("account_id", sa.String(), nullable=True),
        sa.Column("token_endpoint", sa.String(), nullable=False),
        sa.Column("client_id", sa.String(), nullable=False),
        sa.Column("client_secret", sa.LargeBinary(), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["server_id"], ["mcp_servers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("server_id", "account_id", postgresql_nulls_not_distinct=True),
    )
    servers = {
        row["name"]: row["id"] for row in _read("SELECT id, name FROM mcp_servers")
    }
    for row in _read(
        "SELECT id, audience, account_id, secrets, scopes, identity, created_at, revoked_at, "
        "revoked_reason FROM vault WHERE kind = 'oauth'"
    ):
        secrets = json.loads(utils.decrypt(bytes(row["secrets"]), "vault.secrets"))
        audience = row["audience"]
        provider = audience if audience.startswith("mcp:") else audience.partition(":")[2]
        refresh = secrets.get("refresh_token", "")
        op.get_bind().execute(
            sa.text(
                "INSERT INTO oauth_connections (id, provider, account_id, refresh_token, scopes, "
                "identity, connected_at, revoked_at, revoked_reason) VALUES (:id, :provider, "
                ":account_id, :refresh_token, :scopes, :identity, :connected_at, :revoked_at, "
                ":revoked_reason)"
            ),
            {
                "id": row["id"],
                "provider": provider,
                "account_id": row["account_id"],
                "refresh_token": (
                    utils.encrypt(refresh.encode(), "oauth_connections.refresh_token")
                    if refresh
                    else b""
                ),
                "scopes": json.dumps(row["scopes"] or []),
                "identity": json.dumps(row["identity"] or {}),
                "connected_at": row["created_at"],
                "revoked_at": row["revoked_at"],
                "revoked_reason": row["revoked_reason"] or "",
            },
        )
        name = audience.partition(":")[2]
        if audience.startswith("mcp:") and "client_id" in secrets and name in servers:
            secret = secrets.get("client_secret", "")
            op.get_bind().execute(
                sa.text(
                    "INSERT INTO mcp_client_registrations (id, server_id, account_id, "
                    "token_endpoint, client_id, client_secret) VALUES (:id, :server_id, "
                    ":account_id, :token_endpoint, :client_id, :client_secret)"
                ),
                {
                    "id": str(uuid7()),
                    "server_id": servers[name],
                    "account_id": row["account_id"],
                    "token_endpoint": secrets["token_endpoint"],
                    "client_id": secrets["client_id"],
                    "client_secret": (
                        utils.encrypt(secret.encode(), "mcp_client_registrations.client_secret")
                        if secret
                        else b""
                    ),
                },
            )
    op.get_bind().execute(sa.text("DELETE FROM vault WHERE kind = 'oauth'"))


def _restore_service_identities() -> None:
    op.create_table(
        "service_identities",
        sa.Column("service", sa.String(), nullable=False),
        sa.Column("identity", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("secrets", sa.LargeBinary(), nullable=False),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("service"),
    )
    for row in _read(
        "SELECT audience, identity, secrets, updated_at FROM vault "
        "WHERE kind IN ('app_key', 'static') AND audience LIKE 'service:%' AND revoked_at IS NULL"
    ):
        secrets = json.loads(utils.decrypt(bytes(row["secrets"]), "vault.secrets"))
        plaintext = json.dumps(secrets, separators=(",", ":"), sort_keys=True).encode()
        op.get_bind().execute(
            sa.text(
                "INSERT INTO service_identities (service, identity, secrets, connected_at) "
                "VALUES (:service, :identity, :secrets, :connected_at)"
            ),
            {
                "service": row["audience"].partition(":")[2],
                "identity": json.dumps(row["identity"] or {}),
                "secrets": utils.encrypt(plaintext, "service_identities.secrets"),
                "connected_at": row["updated_at"],
            },
        )


def _restore_provider_keys() -> None:
    op.create_table(
        "provider_keys",
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("value", sa.LargeBinary(), nullable=False),
        sa.Column("disconnected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_by_account_id", sa.String(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["updated_by_account_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("provider"),
    )
    op.add_column("agent_calls", sa.Column("api_key_provider", sa.String(), nullable=True))
    for row in _read(
        "SELECT id, audience, secrets, identity, updated_at, revoked_at FROM vault "
        "WHERE kind = 'static' AND audience LIKE 'provider:%'"
    ):
        provider = row["audience"].partition(":")[2]
        secrets = json.loads(utils.decrypt(bytes(row["secrets"]), "vault.secrets"))
        value = secrets.get("value", "")
        op.get_bind().execute(
            sa.text(
                "INSERT INTO provider_keys (provider, value, disconnected_at, "
                "updated_by_account_id, updated_at) VALUES "
                "(:provider, :value, :disconnected_at, :pasted_by, :updated_at)"
            ),
            {
                "provider": provider,
                "value": utils.encrypt(value.encode(), "provider_keys.value") if value else b"",
                "disconnected_at": row["revoked_at"],
                "pasted_by": (row["identity"] or {}).get("pasted_by"),
                "updated_at": row["updated_at"],
            },
        )
        op.get_bind().execute(
            sa.text("UPDATE agent_calls SET api_key_provider = :provider WHERE api_key_id = :id"),
            {"provider": provider, "id": row["id"]},
        )
    op.drop_constraint("agent_calls_billing_source_check", "agent_calls", type_="check")
    op.drop_constraint("agent_calls_api_key_id_fkey", "agent_calls", type_="foreignkey")
    op.drop_column("agent_calls", "api_key_id")
    op.create_foreign_key(
        "agent_calls_api_key_provider_fkey", "agent_calls", "provider_keys",
        ["api_key_provider"], ["provider"], ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "agent_calls_billing_source_check",
        "agent_calls",
        "(subscription_id IS NOT NULL) <> (api_key_provider IS NOT NULL)",
        postgresql_not_valid=True,
    )


def _restore_mcp_secrets() -> None:
    op.add_column("mcp_servers", sa.Column("token", sa.LargeBinary(), nullable=True))
    op.add_column("mcp_servers", sa.Column("secret_headers", sa.LargeBinary(), nullable=True))
    tokens: dict[str, str] = {}
    headers: dict[str, dict[str, str]] = {}
    for row in _read(
        "SELECT audience, header, secrets FROM vault "
        "WHERE kind = 'static' AND audience LIKE 'mcp:%' AND revoked_at IS NULL"
    ):
        name = row["audience"].partition(":")[2]
        value = json.loads(utils.decrypt(bytes(row["secrets"]), "vault.secrets"))["value"]
        if row["header"] == "Authorization":
            tokens[name] = value
        else:
            headers.setdefault(name, {})[row["header"]] = value
    for row in _read("SELECT name FROM mcp_servers"):
        name = row["name"]
        token = tokens.get(name, "")
        plaintext = json.dumps(headers.get(name, {}), separators=(",", ":"), sort_keys=True)
        op.get_bind().execute(
            sa.text(
                "UPDATE mcp_servers SET token = :token, secret_headers = :secret_headers "
                "WHERE name = :name"
            ),
            {
                "name": name,
                "token": utils.encrypt(token.encode(), "mcp_servers.token") if token else b"",
                "secret_headers": utils.encrypt(plaintext.encode(), "mcp_servers.secret_headers"),
            },
        )
    op.alter_column("mcp_servers", "token", nullable=False)
    op.alter_column(
        "mcp_servers", "secret_headers", nullable=False, server_default=sa.text("''::bytea")
    )
    op.get_bind().execute(
        sa.text("DELETE FROM vault WHERE kind = 'static' AND audience LIKE 'mcp:%'")
    )


def _restore_subscriptions() -> None:
    op.create_table(
        "provider_subscriptions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("account_id", sa.String(), nullable=False),
        sa.Column("provider_email", postgresql.CITEXT(), nullable=False),
        sa.Column("payload", sa.LargeBinary(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disconnected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider", "account_id", name="provider_subscriptions_provider_account_id_key"
        ),
    )
    for row in _read(
        "SELECT id, audience, account_id, identity, secrets, expires_at, revoked_at, updated_at "
        "FROM vault WHERE kind = 'subscription'"
    ):
        secrets = json.loads(utils.decrypt(bytes(row["secrets"]), "vault.secrets"))
        plaintext = json.dumps(secrets, separators=(",", ":"), sort_keys=True).encode()
        op.get_bind().execute(
            sa.text(
                "INSERT INTO provider_subscriptions (id, provider, account_id, provider_email, "
                "payload, expires_at, disconnected_at, updated_at) VALUES (:id, :provider, "
                ":account_id, :email, :payload, :expires_at, :disconnected_at, :updated_at)"
            ),
            {
                "id": row["id"],
                "provider": row["audience"].partition(":")[2],
                "account_id": row["account_id"],
                "email": (row["identity"] or {}).get("email", ""),
                "payload": utils.encrypt(plaintext, "provider_subscriptions.payload"),
                "expires_at": row["expires_at"],
                "disconnected_at": row["revoked_at"],
                "updated_at": row["updated_at"],
            },
        )
    op.drop_constraint("agent_calls_subscription_id_fkey", "agent_calls", type_="foreignkey")
    op.create_foreign_key(
        "agent_calls_subscription_id_fkey",
        "agent_calls",
        "provider_subscriptions",
        ["subscription_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def _restore_sandbox_secrets() -> None:
    op.create_table(
        "sandbox_secrets",
        sa.Column("identity_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("service", sa.String(), nullable=True),
        sa.Column("subscription_id", sa.String(), nullable=True),
        sa.Column("resource", sa.String(), nullable=False),
        sa.CheckConstraint("(service IS NULL) <> (subscription_id IS NULL)", name="one_source"),
        sa.ForeignKeyConstraint(["identity_id"], ["sandbox_identities.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["service"], ["service_identities.service"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["subscription_id"], ["provider_subscriptions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("identity_id", "name"),
    )
    op.get_bind().execute(
        sa.text(
            "INSERT INTO sandbox_secrets (identity_id, name, service, subscription_id, resource) "
            "SELECT r.identity_id, r.name, "
            "CASE WHEN v.audience LIKE 'service:%' THEN split_part(v.audience, ':', 2) END, "
            "CASE WHEN v.kind = 'subscription' THEN r.secret_id END, r.resource "
            "FROM sandbox_secret_refs r JOIN vault v ON v.id = r.secret_id"
        )
    )
    op.drop_table("sandbox_secret_refs")
