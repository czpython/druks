from conftest import seed_run
from druks.accounts.models import Account
from druks.durable.models import Run
from druks.durable.schemas import RunResponse


def test_run_projects_its_account(db_session):
    account = Account.get_or_create("dev@example.com")
    seed_run(db_session, "run-attr-1", account_id=account.id)
    db_session.flush()

    run = db_session.get(Run, "run-attr-1")
    assert run.account_id == account.id
    assert RunResponse.from_run(run, []).account_username == "dev@example.com"


def test_an_unowned_run_belongs_to_system(db_session):
    seed_run(db_session, "run-attr-2")
    db_session.flush()

    run = db_session.get(Run, "run-attr-2")
    assert run.account_id == "system"
    assert RunResponse.from_run(run, []).account_username == "system"
