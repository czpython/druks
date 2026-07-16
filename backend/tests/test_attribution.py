from conftest import seed_dbos_status, seed_run
from druks.accounts.models import Account
from druks.durable.models import Run
from druks.durable.schemas import RunResponse


def test_resolve_assignee_maps_email_to_account(db_session):
    account = Account.get_or_create("dev@example.com")
    # Stripped at the boundary; citext matches the case.
    assert Account.resolve_assignee(" Dev@Example.com ") == (account.id, "Dev@Example.com")


def test_resolve_assignee_keeps_the_unmatched_email_for_the_trail(db_session):
    Account.get_or_create("dev@example.com")
    assert Account.resolve_assignee(None) == (None, None)
    assert Account.resolve_assignee("ghost@example.com") == (None, "ghost@example.com")


def test_run_projects_the_stamped_account(db_session):
    account = Account.get_or_create("dev@example.com")
    seed_run(db_session, "run-attr-1")
    seed_dbos_status(db_session, "run-attr-1", "finished", account_id=account.id)
    db_session.flush()

    run = db_session.get(Run, "run-attr-1")
    assert run.account_id == account.id
    assert run.account_email == "dev@example.com"
    assert RunResponse.from_run(run, []).account_email == "dev@example.com"


def test_a_legacy_run_reads_unattributed(db_session):
    seed_run(db_session, "run-attr-2")
    seed_dbos_status(db_session, "run-attr-2", "finished")
    db_session.flush()

    run = db_session.get(Run, "run-attr-2")
    assert not run.account_id
    assert not RunResponse.from_run(run, []).account_email
