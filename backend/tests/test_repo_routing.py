from druks.build.models import Project, ProjectRepo


def _register(db_session, *full_names):
    for full_name in full_names:
        project = Project.create(name=full_name)
        ProjectRepo.create(project_id=project.id, full_name=full_name)
    db_session.flush()


def _lookup(**signals):
    defaults = {"project_name": None, "labels": []}
    return ProjectRepo.lookup(**{**defaults, **signals})


def test_project_name_wins_over_labels(db_session):
    _register(db_session, "acme/widget", "octo/alfred")
    row = _lookup(project_name="widget", labels=["alfred"])
    assert row.full_name == "acme/widget"


def test_label_routes_when_project_name_is_not_a_repo(db_session):
    """The org-project shape: the Jira project names the org, not a repo, and
    SHRP tickets carry a free-form 'Alfred' label — matched case-insensitively."""
    _register(db_session, "octo/alfred")
    row = _lookup(project_name="Octo", labels=["customer-request", "Alfred"])
    assert row.full_name == "octo/alfred"


def test_first_matching_label_wins(db_session):
    _register(db_session, "octo/alfred", "octo/obrv2")
    row = _lookup(labels=["obrv2", "Alfred"])
    assert row.full_name == "octo/obrv2"


def test_no_signal_matches_any_repo(db_session):
    _register(db_session, "octo/alfred")
    assert _lookup(project_name="Octo", labels=["bug"]) is None
