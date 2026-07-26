from druks.accounts.models import Account
from druks.durable.models import Run
from druks.durable.schemas import RunResponse
from druks.testing import seed_run
from druks_field_notes.workflows import Summarize


def test_run_projects_its_account(druks_db):
    account = Account.get_or_create("dev@example.com")
    seed_run(druks_db, kind=Summarize.kind, run_id="run-attr-1", account_id=account.id)
    druks_db.flush()

    run = druks_db.get(Run, "run-attr-1")
    assert run.account_id == account.id
    response = RunResponse.from_run(run, [], input_request=None)
    assert response.account_username == "dev@example.com"


def test_an_unowned_run_belongs_to_system(druks_db):
    seed_run(druks_db, kind=Summarize.kind, run_id="run-attr-2")
    druks_db.flush()

    run = druks_db.get(Run, "run-attr-2")
    assert run.account_id == "system"
    response = RunResponse.from_run(run, [], input_request=None)
    assert response.account_username == "system"
