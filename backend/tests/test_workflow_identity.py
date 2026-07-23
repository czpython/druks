import pytest
from druks.build.workflows import BuildWorkflow, Profile, Scope
from druks.core.workflows import RefreshTokens
from druks.durable.enums import RunState
from druks.durable.exceptions import WorkflowError
from druks.durable.models import Run
from druks.durable.schemas import get_display_label
from druks.events.models import Event
from druks.extensions import loader as extensions_loader
from druks.extensions.exceptions import MalformedExtension
from druks.extensions.loader import register_workflow_package, resolve_workflow_extension
from druks.extensions.registry import workflows
from druks.workflows import Workflow, _log_run_event, step


@pytest.fixture(autouse=True)
def _isolated_registrations():
    # Every test here mutates the package-owner map and the workflows registry;
    # restore both so the suite keeps seeing only the installed extensions.
    packages = dict(extensions_loader._workflow_packages)
    items = dict(workflows._items)
    yield
    extensions_loader._workflow_packages.clear()
    extensions_loader._workflow_packages.update(packages)
    workflows._items = items


def _workflow(name: str, module: str, **attrs):
    async def run(self) -> None: ...

    return type(name, (Workflow,), {"__module__": module, "run": run, **attrs})


def test_registration_is_idempotent_and_conflicts_loudly():
    register_workflow_package("alpha_pkg", "alpha")
    register_workflow_package("alpha_pkg", "alpha")
    with pytest.raises(MalformedExtension, match="already belongs"):
        register_workflow_package("alpha_pkg", "beta")


def test_overlapping_ownership_across_owners_is_rejected():
    register_workflow_package("alpha_pkg", "alpha")
    with pytest.raises(MalformedExtension, match="overlaps"):
        register_workflow_package("alpha_pkg.nested", "beta")


def test_resolution_matches_package_boundaries():
    register_workflow_package("alpha_pkg", "alpha")
    assert resolve_workflow_extension("alpha_pkg.workflows") == "alpha"
    with pytest.raises(LookupError):
        resolve_workflow_extension("alpha_pkg_sibling.workflows")


def test_unregistered_module_fails_at_class_definition():
    # The error carries the invariant: load through the loader, or register first.
    with pytest.raises(WorkflowError, match="druks.extensions.loader"):
        _workflow("Orphan", "nowhere.workflows")


def test_declaring_extension_namespaces_the_kind():
    register_workflow_package("alpha_pkg", "alpha")
    register_workflow_package("beta_pkg", "beta")

    alpha = _workflow("Summarize", "alpha_pkg.workflows")
    beta = _workflow("Summarize", "beta_pkg.workflows")

    # Two extensions can share a local workflow name without colliding on kind.
    assert (alpha.extension, alpha.kind) == ("alpha", "alpha.summarize")
    assert (beta.extension, beta.kind) == ("beta", "beta.summarize")


def test_explicit_kind_is_a_local_suffix():
    register_workflow_package("alpha_pkg", "alpha")
    digest = _workflow("Summarize", "alpha_pkg.workflows", kind="digest")
    assert digest.kind == "alpha.digest"


def test_dotted_explicit_kind_is_rejected():
    register_workflow_package("alpha_pkg", "alpha")
    with pytest.raises(WorkflowError, match="local name"):
        _workflow("Summarize", "alpha_pkg.workflows", kind="alpha.digest")


def test_none_owned_package_keeps_bare_kinds():
    register_workflow_package("plain_pkg", None)
    flow = _workflow("Sweep", "plain_pkg.workflows")
    assert flow.extension is None
    assert flow.kind == "sweep"


def test_in_tree_identities_are_stable():
    # These kinds are durable identities (DBOS workflow names, settings keys,
    # dedup prefixes, step-name prefixes) — byte-for-byte pins.
    assert BuildWorkflow.kind == "build.build_workflow"
    assert Scope.kind == "build.scope"
    assert Profile.kind == "build.profile"
    assert RefreshTokens.kind == "core.refresh_tokens"
    assert (BuildWorkflow.extension, RefreshTokens.extension) == ("build", "core")


def test_steps_capture_the_namespaced_kind():
    # _wrap_steps closes over cls.kind after the namespace lands, so durable
    # step names carry the final kind — the replay identity.
    register_workflow_package("alpha_pkg", "alpha")

    async def run_multistep(self) -> None:
        await self.ping()

    @step
    async def ping(self) -> None: ...

    flow = type(
        "Pinger",
        (Workflow,),
        {"__module__": "alpha_pkg.workflows", "run_multistep": run_multistep, "ping": ping},
    )

    captured = [cell.cell_contents for cell in flow.ping.__closure__]
    assert "alpha.pinger" in captured


def test_lifecycle_event_stamps_the_declaring_extension(db_session):
    # The event's extension derives from the run's kind through the registry —
    # never an argument, never a stored copy on the run.
    register_workflow_package("alpha_pkg", "alpha")
    flow = _workflow("Beacon", "alpha_pkg.workflows")
    run = Run(id="wf-identity-1", kind=flow.kind)
    db_session.add(run)
    db_session.flush()

    payload = _log_run_event(run, RunState.FINISHED, {"type": "note", "id": 1})

    event = db_session.query(Event).filter_by(type="run.finished").one()
    assert payload["run"] == run.id
    assert event.extension == "alpha"


def test_display_label_reads_the_local_kind():
    assert get_display_label("field_notes.summarize") == "Summarize"
