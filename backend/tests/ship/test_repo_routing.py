from druks.contrib.ship.models import Project, ProjectRepo


async def _register(druks_db, *full_names):
    for full_name in full_names:
        project = await Project.create(name=full_name)
        await ProjectRepo.create(project_id=project.id, full_name=full_name)
    await druks_db.flush()


async def _lookup(**signals):
    defaults = {"project_name": None, "labels": []}
    return await ProjectRepo.lookup(**{**defaults, **signals})


async def test_project_name_wins_over_labels(druks_db):
    await _register(druks_db, "acme/widget", "octo/alfred")
    row = await _lookup(project_name="widget", labels=["alfred"])
    assert row.full_name == "acme/widget"


async def test_label_routes_when_project_name_is_not_a_repo(druks_db):
    """The org-project shape: the Jira project names the org, not a repo, and
    SHRP tickets carry a free-form 'Alfred' label — matched case-insensitively."""
    await _register(druks_db, "octo/alfred")
    row = await _lookup(project_name="Octo", labels=["customer-request", "Alfred"])
    assert row.full_name == "octo/alfred"


async def test_first_matching_label_wins(druks_db):
    await _register(druks_db, "octo/alfred", "octo/obrv2")
    row = await _lookup(labels=["obrv2", "Alfred"])
    assert row.full_name == "octo/obrv2"


async def test_no_signal_matches_any_repo(druks_db):
    await _register(druks_db, "octo/alfred")
    assert await _lookup(project_name="Octo", labels=["bug"]) is None


async def test_siblings_returns_only_other_repos_in_the_project(druks_db):
    project = await Project.create(name="Acme")
    target = await ProjectRepo.create(project_id=project.id, full_name="acme/api")
    sibling = await ProjectRepo.create(
        project_id=project.id,
        full_name="acme/web",
        purpose="frontend",
    )
    other_project = await Project.create(name="Other")
    await ProjectRepo.create(project_id=other_project.id, full_name="other/worker")

    assert await target.siblings() == [sibling]
