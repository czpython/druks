import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from druks.accounts.models import Account
from druks.sandbox.models import SandboxIdentity
from druks.testing import seed_run
from sqlalchemy import text

_MIGRATION = (
    Path(__file__).resolve().parents[1] / "migrations/versions/d3a7f1c9e5b2_chat_platform.py"
)


def migrate(connection, direction: str) -> None:
    spec = importlib.util.spec_from_file_location("chat_platform", _MIGRATION)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with Operations.context(MigrationContext.configure(connection)):
        getattr(migration, direction)()


async def test_platform_migration_preserves_run_owners_and_allows_chat_identities(druks_db):
    run = await seed_run(druks_db, kind="test", run_id="run-chat-migration")
    account = await Account.get_for_run(druks_db, run.account_id)
    identity, _ = await SandboxIdentity.create(
        druks_db, account_id=account.id, run_id=run.id, scoped_to="workflow", secret_refs=[]
    )
    for statement in (
        "DROP TABLE chat_messages, chat_conversations",
        "ALTER TABLE sandbox_identities DROP COLUMN account_id",
        "ALTER TABLE sandbox_identities ALTER COLUMN run_id SET NOT NULL",
    ):
        await druks_db.execute(text(statement))
    connection = await druks_db.connection()

    await connection.run_sync(migrate, "upgrade")

    assert (
        await druks_db.execute(
            text("SELECT account_id FROM sandbox_identities WHERE id=:id"), {"id": identity.id}
        )
    ).scalar_one() == account.id
    await druks_db.execute(
        text(
            "INSERT INTO sandbox_identities "
            "(id, account_id, run_id, scoped_to, token_hash, created_at, expires_at) "
            "VALUES ('chat-migration', :account_id, NULL, 'chat', '\\x00', now(), now())"
        ),
        {"account_id": account.id},
    )
    assert (await druks_db.execute(text("SELECT to_regclass('chat_conversations')"))).scalar()

    await connection.run_sync(migrate, "downgrade")

    assert not (await druks_db.execute(text("SELECT to_regclass('chat_conversations')"))).scalar()
    assert (await druks_db.execute(text("SELECT count(*) FROM sandbox_identities"))).scalar() == 1
