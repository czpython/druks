from conftest import seed_dbos_status, seed_run
from druks.accounts.models import Account
from druks.build.workflows import assignee_attribution
from druks.durable.models import Run
from druks.durable.schemas import RunResponse


def test_assignee_attribution_resolves_the_account(db_session):
    account = Account.get_or_create("dev@example.com")
    # Stripped at the boundary; citext matches the case.
    assert assignee_attribution(" Dev@Example.com ") == {"account_id": account.id}


def test_assignee_attribution_records_why_no_account_resolved(db_session):
    Account.get_or_create("dev@example.com")
    assert assignee_attribution(None) == {"unattributed_reason": "missing_assignee"}
    assert assignee_attribution("ghost@example.com") == {
        "unattributed_reason": "unmatched_assignee"
    }


def test_run_projects_the_stamped_account(db_session):
    account = Account.get_or_create("dev@example.com")
    seed_run(db_session, "run-attr-1")
    seed_dbos_status(db_session, "run-attr-1", "finished", account_id=account.id)
    db_session.flush()

    run = db_session.get(Run, "run-attr-1")
    assert run.account_id == account.id
    response = RunResponse.from_run(run, [], {account.id: account.email})
    assert response.account == "dev@example.com"


def test_a_legacy_run_reads_unattributed(db_session):
    seed_run(db_session, "run-attr-2")
    seed_dbos_status(db_session, "run-attr-2", "finished")
    db_session.flush()

    run = db_session.get(Run, "run-attr-2")
    assert not run.account_id
    assert not RunResponse.from_run(run, []).account
