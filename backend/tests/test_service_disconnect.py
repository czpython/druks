from conftest import bind_ambient_session, connect_service
from druks.accounts.models import Account
from druks.mcp.models import McpServer
from druks.secrets.datastructures import Audience
from druks.secrets.models import VaultSecret


async def test_disconnect_revokes_the_card_and_every_sign_in_of_that_service_only(
    druks_db, druks_client
):
    bind_ambient_session(druks_db)
    card = await connect_service(
        "github", identity={"app_id": "123"}, secrets={"private_key": "private"}
    )
    other = await connect_service(
        "github_reviewer", identity={"app_id": "456"}, secrets={"private_key": "reviewer"}
    )
    account = await Account.get_or_create(druks_db, "one@example.com")
    connection = await VaultSecret.connect(
        druks_db,
        Audience.service("github"),
        account_id=account.id,
        refresh_token="refresh",
        scopes=[],
    )

    for _ in range(2):
        assert (await druks_client.delete("/api/services/github")).status_code == 204

    for row in (card, other, connection):
        await druks_db.refresh(row)
    assert card.revoked_at
    assert other.is_live
    assert connection.revoked_reason == "service_disconnected"


async def test_disconnect_revokes_the_login_its_mcp_server_derived(druks_db, druks_client):
    bind_ambient_session(druks_db)
    login = await connect_service(
        "jira",
        identity={"email": "svc@example.com", "base_url": "https://acme.atlassian.net"},
        secrets={"api_token": "jira-token", "webhook_secret": "hook"},
    )
    await McpServer.create(
        druks_db, name="atlassian", url="https://mcp.atlassian.com/v2/mcp", is_oauth=True
    )
    account = await Account.get_or_create(druks_db, "default@example.com")
    derived = await McpServer.store_login(druks_db, "atlassian", login, account.id)

    assert (await druks_client.delete("/api/services/jira")).status_code == 204

    await druks_db.refresh(derived)
    assert derived.revoked_reason == "service_disconnected"
