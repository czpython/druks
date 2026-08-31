from druks.accounts.models import Account
from druks.durable.models import Run
from druks.durable.schemas import RunResponse
from druks.testing import seed_run
from druks_field_notes.workflows import Summarize


async def test_run_projects_its_account(druks_db):
    account = await Account.get_or_create("dev@example.com")
    await seed_run(druks_db, kind=Summarize.kind, run_id="run-attr-1", account_id=account.id)
    await druks_db.flush()

    run = await druks_db.get(Run, "run-attr-1")
    await run.awaitable_attrs.agent_calls
    assert run.account_id == account.id
    response = RunResponse.from_run(run, input_request=None)
    assert response.account_username == "dev@example.com"


async def test_an_unowned_run_belongs_to_system(druks_db):
    await seed_run(druks_db, kind=Summarize.kind, run_id="run-attr-2")
    await druks_db.flush()

    run = await druks_db.get(Run, "run-attr-2")
    await run.awaitable_attrs.agent_calls
    assert run.account_id == "system"
    response = RunResponse.from_run(run, input_request=None)
    assert response.account_username == "system"
