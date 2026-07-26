from druks.contrib.ship.models import Project, ProjectRepo


def _register(druks_db, *full_names):
    for full_name in full_names:
        project = Project.create(name=full_name)
        ProjectRepo.create(project_id=project.id, full_name=full_name)
    druks_db.flush()


def _lookup(**signals):
    defaults = {"project_name": None, "labels": []}
    return ProjectRepo.lookup(**{**defaults, **signals})


def test_project_name_wins_over_labels(druks_db):
    _register(druks_db, "acme/widget", "octo/alfred")
    row = _lookup(project_name="widget", labels=["alfred"])
    assert row.full_name == "acme/widget"


def test_label_routes_when_project_name_is_not_a_repo(druks_db):
    """The org-project shape: the Jira project names the org, not a repo, and
    SHRP tickets carry a free-form 'Alfred' label — matched case-insensitively."""
    _register(druks_db, "octo/alfred")
    row = _lookup(project_name="Octo", labels=["customer-request", "Alfred"])
    assert row.full_name == "octo/alfred"


def test_first_matching_label_wins(druks_db):
    _register(druks_db, "octo/alfred", "octo/obrv2")
    row = _lookup(labels=["obrv2", "Alfred"])
    assert row.full_name == "octo/obrv2"


def test_no_signal_matches_any_repo(druks_db):
    _register(druks_db, "octo/alfred")
    assert _lookup(project_name="Octo", labels=["bug"]) is None


def test_siblings_returns_only_other_repos_in_the_project(druks_db):
    project = Project.create(name="Acme")
    target = ProjectRepo.create(project_id=project.id, full_name="acme/api")
    sibling = ProjectRepo.create(
        project_id=project.id,
        full_name="acme/web",
        purpose="frontend",
    )
    other_project = Project.create(name="Other")
    ProjectRepo.create(project_id=other_project.id, full_name="other/worker")

    assert target.siblings() == [sibling]
