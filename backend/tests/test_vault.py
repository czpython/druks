from datetime import UTC, datetime, timedelta

import pytest
from druks.accounts.models import Account
from druks.apps.registry import services
from druks.database import db_session
from druks.mcp import oauth
from druks.secrets.enums import SecretKind
from druks.secrets.exceptions import SecretRevokedError
from druks.secrets.models import VaultSecret
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy_encrypted_field import SecretDecryptError, utils


async def _static(audience: str = "mcp:linear", **fields) -> VaultSecret:
    row = VaultSecret(
        kind=SecretKind.STATIC,
        audience=audience,
        header="Authorization",
        secrets={"value": "lin_secret"},
        **fields,
    )
    db_session().add(row)
    await db_session().flush()
    return row


async def test_a_pasted_value_issues_itself_with_no_expiry(druks_db):
    row = await _static()

    assert await row.issue_token("") == ("lin_secret", None)
    assert await VaultSecret.lookup(SecretKind.STATIC, "mcp:linear", header="Authorization") is row


async def test_one_row_per_audience_account_and_header_except_an_oauth_connection(druks_db):
    await _static()
    with pytest.raises(IntegrityError):
        await _static()
    await db_session().rollback()

    account = await Account.get_or_create("op@example.com")
    for _ in range(2):
        db_session().add(
            VaultSecret(
                kind=SecretKind.OAUTH,
                audience="gmail",
                account_id=account.id,
                secrets={"refresh_token": "rt"},
            )
        )
    await db_session().flush()

    assert len(await VaultSecret.list_connections("gmail")) == 2


async def test_a_revoked_secret_keeps_its_facts_and_loses_its_secrets(druks_db):
    row = await _static(identity={"slug": "linear"})

    await row.revoke("user")
    await row.revoke("server_removed")

    assert row.revoked_at and row.revoked_reason == "user"
    assert dict(row.secrets) == {}
    assert row.identity == {"slug": "linear"}
    assert not (await VaultSecret.get(row.id)).is_live
    assert await VaultSecret.lookup(SecretKind.STATIC, "mcp:linear", header="Authorization") is None


async def test_the_secrets_column_is_ciphertext_bound_to_the_vault(druks_db):
    row = await _static()

    stored = await db_session().scalar(
        text("SELECT secrets FROM vault WHERE id = :id"), {"id": row.id}
    )

    assert b"lin_secret" not in bytes(stored)
    assert b"lin_secret" in utils.decrypt(bytes(stored), "vault.secrets")
    with pytest.raises(SecretDecryptError):
        utils.decrypt(bytes(stored), "another_table.secrets")


@pytest.mark.parametrize("kind", list(SecretKind))
async def test_a_revoked_secret_issues_nothing(druks_db, kind):
    row = VaultSecret(kind=kind, audience="mcp:linear", secrets={"value": "x"})
    db_session().add(row)
    await db_session().flush()
    await row.revoke("user")

    with pytest.raises(SecretRevokedError, match="mcp:linear"):
        await row.issue_token("")


async def test_a_service_grant_issues_through_the_services_client(druks_db, monkeypatch):
    row = await VaultSecret.connect("service:acme", account_id=None, refresh_token="rt", scopes=[])
    expiry = datetime.now(UTC) + timedelta(hours=1)

    class Client:
        async def get_access_token(self, *, connection):
            assert connection is row
            return "tok", expiry

    class Acme:
        @staticmethod
        async def get_oauth_client():
            return Client()

    monkeypatch.setattr(services, "get", lambda slug: Acme if slug == "acme" else None)

    assert await row.issue_token("") == ("tok", expiry)


async def test_an_mcp_grant_issues_through_the_servers_client(druks_db, monkeypatch):
    row = await VaultSecret.connect("mcp:linear", account_id=None, refresh_token="rt", scopes=[])

    async def get_access_token(name, account_id):
        assert (name, account_id) == ("linear", None)
        return "tok", None

    monkeypatch.setattr(oauth, "get_access_token", get_access_token)

    assert await row.issue_token("") == ("tok", None)
