from druks.contrib.software_factory.models import Project, ProjectRepo, WorkItem
from druks.contrib.software_factory.workflows import Build
from druks.testing import seed_run
from uuid_utils import uuid7


async def make_test_work_item(*, repo: str, **kwargs):
    """A WorkItem with the Project / ProjectRepo binding it requires. Every item
    carries a ticket key, unique per source — tests that don't care get one."""
    project = await Project.get_for_repo(repo)
    if not project:
        project = await Project.create(name=repo)
        await ProjectRepo.create(project_id=project.id, full_name=repo)
    kwargs.setdefault("ticket_key", f"TEST-{uuid7()}")
    return await WorkItem.create(project_id=project.id, repo=repo, **kwargs)


async def seed_build_run(
    session,
    *,
    work_item_id: int,
    state: str = "running",
    input_gate: str | None = None,
    input_request: dict | None = None,
    failure: str | None = None,
    account_id: str | None = None,
):
    """Seed a build Run for a work item. The run and its calls are the item's
    timeline; it finds the item through the subject it was started for."""
    if state == "parked" and not input_gate:
        input_gate = "review"  # a parked run always has a gate; derivation needs it
    item = await WorkItem.get(work_item_id)
    run = await seed_run(
        session,
        kind=Build.kind,
        subject=item,
        state=state,
        input_gate=input_gate,
        input_request=input_request,
        failure=failure,
        account_id=account_id or "system",
    )
    await session.flush()
    return run
