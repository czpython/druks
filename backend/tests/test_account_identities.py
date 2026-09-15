from druks.accounts.models import Account
from druks.database import db_session
from druks.secrets.enums import IdentityStatus
from druks.secrets.models import VaultSecret

AUTHORITY = "https://issuer.example.com"


async def _grant(account, *, subject="user-1", authority=AUTHORITY):
    return await VaultSecret.connect(
        "mcp:company_tracker",
        account_id=account.id if account else None,
        refresh_token="refresh-token",
        scopes=["openid", "email"],
        identity={"authority": authority, "subject": subject, "source": "id_token"},
        identity_status=IdentityStatus.RESOLVED,
    )


async def test_provider_user_resolves_the_connection_owner(druks_db):
    owner = await Account.get_or_create(druks_db, "github-boss")
    await _grant(owner)

    assert await Account.lookup(druks_db, AUTHORITY, "user-1") is owner


async def test_equal_subject_at_another_provider_does_not_match(druks_db):
    owner = await Account.get_or_create(druks_db, "github-boss")
    await _grant(owner, authority="https://other.example.com")

    assert not await Account.lookup(druks_db, AUTHORITY, "user-1")


async def test_shared_grant_has_no_owner(druks_db):
    owner = await Account.get_or_create(druks_db, "github-boss")
    await _grant(None)
    assert not await Account.lookup(druks_db, AUTHORITY, "user-1")

    await _grant(owner)
    assert await Account.lookup(druks_db, AUTHORITY, "user-1") is owner


async def test_repeated_connections_for_one_account_are_unambiguous(druks_db):
    owner = await Account.get_or_create(druks_db, "github-boss")
    await _grant(owner)
    await _grant(owner)

    assert await Account.lookup(druks_db, AUTHORITY, "user-1") is owner


async def test_conflicting_owners_prevent_attribution(druks_db, caplog):
    owner = await Account.get_or_create(druks_db, "github-boss")
    other = await Account.get_or_create(druks_db, "other-login")
    await _grant(owner)
    second = await _grant(other)

    assert not await Account.lookup(druks_db, AUTHORITY, "user-1")
    assert "multiple accounts" in caplog.text

    await second.revoke("user", session=db_session())
    assert await Account.lookup(druks_db, AUTHORITY, "user-1") is owner
