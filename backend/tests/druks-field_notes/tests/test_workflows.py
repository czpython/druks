from types import SimpleNamespace
from unittest import mock

from conftest import connect_service
from druks.sandbox.layout import get_repo_root
from druks.testing import run_workflow
from druks_field_notes.app import FieldNotes
from druks_field_notes.contracts import GistOutput
from druks_field_notes.models import Note, Repository
from druks_field_notes.workflows import Summarize, Survey


async def test_summarize_writes_the_gist(druks_db, monkeypatch):
    note = await Note.create(body="the pump ran hot on the second pass")
    summarize = mock.AsyncMock(return_value=GistOutput(gist="The pump ran hot on the second pass."))
    monkeypatch.setattr(FieldNotes, "summarize", staticmethod(summarize))

    await run_workflow(Summarize, subject=note)

    summarize.assert_awaited_once_with(note_body=note.body)
    assert (await Note.get(note.id)).gist == "The pump ran hot on the second pass."


async def test_dispatch_starts_the_workflow_for_the_note(monkeypatch):
    note = Note(id=42)
    start = mock.AsyncMock(return_value="run-1")
    monkeypatch.setattr(Summarize, "start", staticmethod(start))

    run_id = await Summarize.dispatch(note=note)

    assert run_id == "run-1"
    start.assert_awaited_once_with(subject=note)


async def test_survey_writes_the_repository_gist(druks_db, monkeypatch):
    repository = await Repository.create(repo="acme/widgets")
    survey = mock.AsyncMock(return_value=GistOutput(gist="Widgets for every shelf."))
    monkeypatch.setattr(FieldNotes, "survey", staticmethod(survey))

    await run_workflow(Survey, subject=repository)

    survey.assert_awaited_once_with()
    assert (await Repository.get(repository.id)).gist == "Widgets for every shelf."


async def test_survey_workspace_clones_the_subject_repo(druks_db):
    workflow = Survey()
    workflow.subject = await Repository.create(repo="acme/widgets")
    workflow.account_id = None
    host = SimpleNamespace(id="h1", ssh_username="exedev")

    workspace = await workflow.get_workspace(host)

    assert (workspace.get_repo(workspace.subject), workspace.branch) == ("acme/widgets", None)
    assert workspace.repo_path == get_repo_root("exedev")
    # The box's secret names the operator's vault row and the repo before the box exists.
    row = await connect_service(
        "github", identity={"app_id": "1", "slug": "druks-operator"}, secrets={"private_key": "pem"}
    )
    [secret] = await workflow.get_secret_refs()
    assert secret.key == ("github", row.id, "acme/widgets", "")
