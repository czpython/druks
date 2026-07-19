import json
import sys
from importlib.metadata import EntryPoint
from pathlib import Path

import pytest
from druks.extensions import loader
from druks.extensions.loader import load_extension
from druks.workflows import WorkflowStartResult

# druks-field_notes is a real, standalone extension package that lives outside the
# druks tree (``backend/tests/druks-field_notes``) — a model, a migration, a route, a
# workflow, a subscriber, and settings, hand-written as the reference an author copies.
# CI installs it editable and these tests drive every surface through the platform's own
# loader: boot, discovery, migrations, routes, run start, feed formatting, and settings.
# They prove druks is platform-shaped around a separately-shipped package, not only its
# built-ins.
#
# The package is exposed here through a real ``importlib.metadata.EntryPoint`` over its
# on-disk source, the same wiring an editable ``pip install -e`` produces — so the suite
# discovers it without a global install polluting the in-tree extension set. The CI
# ``pip install -e`` step is the belt-and-suspenders proof the packaging itself resolves.
_PACKAGE = "druks_field_notes"
_PACKAGE_ROOT = Path(__file__).resolve().parent / "druks-field_notes"


def _entry() -> EntryPoint:
    return EntryPoint(
        name="field_notes",
        value=f"{_PACKAGE}.extension:FieldNotes",
        group="druks.extensions",
    )


def _extension_setting_field(extension: type, name: str) -> dict:
    """One of the extension's declared settings, as the settings API serializes it —
    aliased to the wire shape the frontend receives."""
    from druks.user_settings.reads import get_extension_settings

    projected = get_extension_settings(extension)
    field = next(f for f in projected.settings if f.name == name)
    return field.model_dump(by_alias=True)


@pytest.fixture(scope="module")
def external_package():
    """Put the on-disk package on ``sys.path`` and restore every global its load
    mutates (table metadata, capability registries, signal receivers) so the rest of
    the suite sees the in-tree extensions untouched. Module-scoped so its mapped
    ``Note`` class is declared once — a re-declaration into the shared metadata
    collides."""
    from blinker import signal
    from druks.extensions import loader as extensions_loader
    from druks.extensions.registry import agents, webhooks, workflows
    from druks.models import Base

    sys.path.insert(0, str(_PACKAGE_ROOT))

    tables = set(Base.metadata.tables)
    registries = {registry: dict(registry._items) for registry in (agents, webhooks, workflows)}
    packages = dict(extensions_loader._workflow_packages)
    finished = signal("run.finished")
    receivers = dict(finished.receivers)
    try:
        yield
    finally:
        sys.path.remove(str(_PACKAGE_ROOT))
        for name in set(Base.metadata.tables) - tables:
            Base.metadata.remove(Base.metadata.tables[name])
        for registry, snapshot in registries.items():
            registry._items = snapshot
        extensions_loader._workflow_packages.clear()
        extensions_loader._workflow_packages.update(packages)
        finished.receivers = receivers
        for name in [m for m in sys.modules if m == _PACKAGE or m.startswith(f"{_PACKAGE}.")]:
            del sys.modules[name]


@pytest.fixture
def installed(external_package, monkeypatch):
    """The field_notes entry point as the only one the loader sees, so discovery
    resolves the proof package exactly as an install would."""
    monkeypatch.setattr(loader, "entry_points", lambda *, group: [_entry()])


def test_boot_loads_the_external_extension(installed):
    """The out-of-tree package boots through the app-less loader — its entry point
    resolves and its class is the loaded Extension."""
    extension = load_extension("field_notes")

    assert extension.name == "field_notes"
    assert extension.package == _PACKAGE
    assert extension.subject_type == "note"


def test_discovery_registers_the_tables_and_capabilities(installed):
    """Loading imports the package's models and walks its capability modules, so its
    prefixed table and its workflow both register."""
    from druks.models import Base

    extension = load_extension("field_notes")

    assert "field_notes_notes" in Base.metadata.tables
    assert [workflow.__name__ for workflow in extension.workflows()] == ["Summarize"]

    capability_modules = {module.__name__ for module in extension.capability_modules()}
    assert f"{_PACKAGE}.subscribers" in capability_modules


def test_routes_read_off_the_loaded_extension(installed):
    """The extension's own router plus the free read-sides enumerate without a
    FastAPI app."""
    extension = load_extension("field_notes")

    prefixes = {router.prefix for router in extension.routers()}
    assert "/notes" in prefixes  # the extension's declared router
    assert "/transcripts/{call_id}" in prefixes  # the free agent-call read-side
    assert "/note" in prefixes  # the subject read-side it gets for declaring subject_type


def test_settings_resolve_off_the_loaded_extension(installed):
    """The extension's declared Settings resolve through the override store, and an
    operator override round-trips."""
    extension = load_extension("field_notes")

    settings_model = extension.settings_model
    assert settings_model is not None
    assert list(settings_model.model_fields) == ["board_size", "visibility", "sync_token"]
    assert extension.settings().board_size == 50

    extension.override_setting("board_size", 5)
    assert extension.settings().board_size == 5
    with pytest.raises(ValueError, match="board_size"):
        extension.override_setting("board_size", 0)  # below the ge=1 bound


def test_enum_setting_rejects_a_value_outside_the_choice_set(installed):
    """A Literal-typed setting only accepts one of its declared choices; anything
    else fails at the write with the field named."""
    extension = load_extension("field_notes")

    extension.override_setting("visibility", "team")
    assert extension.settings().model_dump()["visibility"] == "team"
    with pytest.raises(ValueError, match="visibility"):
        extension.override_setting("visibility", "galaxy")


def test_secret_setting_round_trips_and_stays_redacted_in_the_api(installed):
    """A SecretStr setting stores and resolves its raw value for the extension to use,
    but never surfaces it — the settings API redacts it to a set/unset flag."""
    extension = load_extension("field_notes")

    # Unset by default: no value, but the field is still declared as a secret.
    field = _extension_setting_field(extension, "sync_token")
    assert field["type"] == "secret"
    assert field["value"] is None
    assert field["secretSet"] is False

    # An operator writes it; the extension can read the raw value back to use it.
    extension.override_setting("sync_token", "sk-live-123")
    resolved = extension.settings().model_dump()["sync_token"]
    assert resolved.get_secret_value() == "sk-live-123"

    # But the API still never carries the raw value — only that one is now set.
    field = _extension_setting_field(extension, "sync_token")
    assert field["value"] is None
    assert field["secretSet"] is True
    assert "sk-live-123" not in json.dumps(field)


def _field_notes_settings(client) -> dict[str, dict]:
    body = client.get("/api/settings/extensions").json()
    extension = next(m for m in body["extensions"] if m["name"] == "field_notes")
    return {f["name"]: f for f in extension["settings"]}


def test_settings_round_trip_over_http(installed, tmp_path):
    """Declare → GET → PATCH → GET over the real settings endpoints: the enum exposes
    its choices, the secret stays redacted, a written value round-trips, and an
    out-of-choice value is a field-level 422."""
    from conftest import configure_app_for_test, make_settings
    from fastapi.testclient import TestClient

    load_extension("field_notes")
    with TestClient(configure_app_for_test(settings=make_settings(tmp_path))) as client:
        # GET surfaces the enum's choices and the redacted, unset secret.
        fields = _field_notes_settings(client)
        assert fields["visibility"]["type"] == "enum"
        assert fields["visibility"]["choices"] == ["private", "team", "public"]
        assert fields["visibility"]["value"] == "private"
        assert fields["sync_token"]["type"] == "secret"
        assert fields["sync_token"]["value"] is None
        assert fields["sync_token"]["secretSet"] is False

        # PATCH both; the enum move and the secret write persist.
        patch = client.patch(
            "/api/settings/extensions",
            json={
                "extensionSettings": {
                    "field_notes": {"visibility": "team", "sync_token": "sk-live-999"}
                }
            },
        )
        assert patch.status_code == 200

        fields = _field_notes_settings(client)
        assert fields["visibility"]["value"] == "team"
        assert fields["visibility"]["overridden"] is True
        # The secret reads back as set, never as its raw value — including in the
        # PATCH response body itself.
        assert fields["sync_token"]["secretSet"] is True
        assert fields["sync_token"]["value"] is None
        assert "sk-live-999" not in patch.text

        # An out-of-choice enum value is rejected with the field named — and the
        # rejected value is not echoed back.
        rejected = client.patch(
            "/api/settings/extensions",
            json={"extensionSettings": {"field_notes": {"visibility": "galaxy"}}},
        )
        assert rejected.status_code == 422
        assert "visibility" in rejected.json()["detail"]
        assert "galaxy" not in rejected.text

        # A too-short secret is rejected server-side, and its raw value never appears
        # in the 422 body (the validation error carries only the field + reason).
        bad_secret = client.patch(
            "/api/settings/extensions",
            json={"extensionSettings": {"field_notes": {"sync_token": "sk-tiny"}}},
        )
        assert bad_secret.status_code == 422
        assert "sync_token" in bad_secret.json()["detail"]
        assert "sk-tiny" not in bad_secret.text

        # A long-enough secret that fails the extension's own custom validator: its
        # message names the raw value, but the 422 body must still not carry it.
        bad_format = client.patch(
            "/api/settings/extensions",
            json={"extensionSettings": {"field_notes": {"sync_token": "malformed-token-value"}}},
        )
        assert bad_format.status_code == 422
        assert "sync_token" in bad_format.json()["detail"]
        assert "malformed-token-value" not in bad_format.text


def test_extension_with_a_nested_model_setting_is_rejected_at_declaration():
    """A Settings field that is a nested model is outside the flat settings contract —
    it fails loudly when the extension class is declared, not at the first PATCH."""
    from druks.extensions import Extension
    from druks.extensions.exceptions import SettingsDeclarationError
    from pydantic import BaseModel

    class _Credentials(BaseModel):
        token: str = ""

    with pytest.raises(SettingsDeclarationError, match="credentials"):

        class _BadExtension(Extension):
            name = "bad_nested_settings"

            class Settings(BaseModel):
                credentials: _Credentials = _Credentials()


def test_migration_is_the_history_root(installed):
    """The extension ships its own migration history under
    ``<package>/migrations``, rooted at a down_revision-less baseline the platform
    runs under ``alembic_version_field_notes``."""
    extension = load_extension("field_notes")

    package_dir = extension.package_dir()
    assert package_dir is not None
    assert extension.migrations_dir() == package_dir / "migrations"
    (baseline,) = (package_dir / "migrations" / "versions").glob("*.py")
    assert baseline.name.startswith("field_notes_")


def test_feed_formats_the_extensions_event(installed):
    """The extension renders its own domain event into an activity-feed row —
    core dispatches to it by ``event.extension`` and never learns its event types."""
    from druks.events.models import Event

    extension = load_extension("field_notes")
    event = Event(
        id=1,
        type="run.finished",
        subject_type="note",
        subject_id="7",
        extension="field_notes",
        payload={},
        created_at=Event.utc_now(),
    )

    item = extension.format_event(event)

    assert item.source == "field_notes"
    assert "note 7" in item.summary
    assert item.link_path == "/app/field_notes/notes/7"


async def test_dispatch_starts_a_run_keyed_to_the_note(installed, monkeypatch):
    """The workflow's launch policy starts one run per note — the subject keys off
    the note id, and the note id also rides the run as its typed input. The
    workflow's identity comes from its declaring extension, never the caller."""
    load_extension("field_notes")
    from druks_field_notes.workflows import Summarize

    assert Summarize.extension == "field_notes"
    assert Summarize.kind == "field_notes.summarize"

    started: dict[str, object] = {}

    async def _capture(*, subject, **input):
        started.update(subject=subject, input=input)
        return WorkflowStartResult(run_id="run-1", is_duplicate=False)

    monkeypatch.setattr(Summarize, "start", _capture)

    run_id = await Summarize.dispatch(note_id=42)

    assert run_id == "run-1"
    assert started["subject"] == {"type": "note", "id": 42}
    assert started["input"] == {"note_id": 42}


async def test_run_summarizes_the_note_and_saves_it(installed, db_session, monkeypatch):
    """The workflow body is a real single operation: the agent produces the summary
    prose and the run persists it onto the note. The agent is stubbed — the harness
    and VM are the platform's concern, out of scope for the proof."""
    load_extension("field_notes")
    from druks_field_notes.contracts import NoteSummary
    from druks_field_notes.extension import FieldNotes
    from druks_field_notes.models import Note
    from druks_field_notes.workflows import Summarize

    # The session-wide schema was built before the proof extension's table was in the
    # metadata; create it on the test's own connection so it rolls back with the txn.
    Note.__table__.create(bind=db_session.bind, checkfirst=True)
    note = Note.create(body="ran the loader against a real out-of-tree package")

    async def _summarize(**_context):
        return NoteSummary(summary="Proved the loader on an external package.")

    monkeypatch.setattr(FieldNotes, "summarize", staticmethod(_summarize))

    await Summarize.run.__wrapped__(Summarize(), note_id=note.id)

    saved = Note.get(note.id)
    assert saved is not None
    assert saved.summary == "Proved the loader on an external package."
