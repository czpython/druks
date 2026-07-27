from druks.contrib.ship.models import Project, ProjectRepo, WorkItem
from druks.contrib.ship.workflows import Build
from druks.testing import seed_run
from uuid_utils import uuid7


def make_test_work_item(*, repo: str, **kwargs):
    """A WorkItem with the Project / ProjectRepo binding it requires. Every item
    carries a ticket key, unique per source — tests that don't care get one."""
    project = Project.get_for_repo(repo)
    if not project:
        project = Project.create(name=repo)
        ProjectRepo.create(project_id=project.id, full_name=repo)
    kwargs.setdefault("ticket_key", f"TEST-{uuid7()}")
    return WorkItem.create(project_id=project.id, repo=repo, **kwargs)


def seed_build_run(
    session,
    *,
    work_item_id: int,
    state: str = "running",
    input_gate: str | None = None,
    input_request: dict | None = None,
    failure: str | None = None,
    account_id: str | None = None,
):
    """Seed a build Run for a work item and bind it via ``item.build_run_id``. The run
    and its calls are the item's timeline."""
    if state == "parked" and not input_gate:
        input_gate = "review"  # a parked run always has a gate; derivation needs it
    item = WorkItem.get(work_item_id)
    run = seed_run(
        session,
        kind=Build.kind,
        subject=item,
        state=state,
        input_gate=input_gate,
        input_request=input_request,
        failure=failure,
        account_id=account_id or "system",
    )
    item.build_run_id = run.id
    session.flush()
    return run
