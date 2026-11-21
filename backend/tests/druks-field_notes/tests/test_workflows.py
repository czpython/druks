from types import SimpleNamespace
from unittest import mock

from conftest import connect_service
from dbos import DBOS
from druks import ui
from druks.sandbox import SandboxSecret
from druks.sandbox.layout import get_repo_root
from druks.testing import run_workflow, seed_run
from druks_field_notes.app import FieldNotes
from druks_field_notes.contracts import GistOutput
from druks_field_notes.models import Note, Repository
from druks_field_notes.workflows import Summarize, Survey


async def test_dispatch_starts_the_workflow_for_the_note(monkeypatch):
    note = Note(id=42)
    start = mock.AsyncMock(return_value="run-1")
    monkeypatch.setattr(Summarize, "start", staticmethod(start))

    run_id = await Summarize.dispatch(note=note)

    assert run_id == "run-1"
    start.assert_awaited_once_with(subject=note)


async def test_a_review_shows_its_facts_and_table_on_the_dashboard_and_in_the_approval(
    druks_db, monkeypatch
):
    note = await Note.create(body="Fan noise on rack 3.")
    run = await seed_run(druks_db, kind=Summarize.kind, subject=note)
    workflow = Summarize()
    workflow._workflow_id = run.id
    workflow._subject = note.identity
    shown = {}

    async def call_through(options, function, *args, **kwargs):
        return await function(*args, **kwargs)

    async def answer(*args, **kwargs):
        # What the dashboard and the approval read while the run waits.
        await druks_db.refresh(run)
        shown["ask"] = await run.get_ask()
        shown["approval"] = await run.get_rendered_ask(has_chat=False)
        return {"action": "approve"}

    monkeypatch.setattr(DBOS, "run_step_async", call_through)
    # The approval goes out from a queue of its own.
    monkeypatch.setattr(DBOS, "enqueue_workflow_async", mock.AsyncMock())
    monkeypatch.setattr(DBOS, "recv_async", answer)

    reply = await workflow.review(
        context=[
            ui.Facts([ui.Fact("Body", note.body)]),
            ui.Table(columns=[ui.TableColumn("Rack")], rows=[ui.TableRow(["3"])]),
        ]
    )

    assert reply.action == "approve"
    assert [block["block"] for block in shown["ask"]["context"]] == ["facts", "table"]
    assert shown["approval"]["body"] == (
        "# Review\n\n- **Body**: Fan noise on rack 3.\n\n| Rack |\n| --- |\n| 3 |"
    )


async def test_survey_writes_the_repository_gist(druks_db, monkeypatch):
    repository = await Repository.create(repo="acme/widgets")
    survey = mock.AsyncMock(return_value=GistOutput(gist="Widgets for every shelf."))
    monkeypatch.setattr(FieldNotes, "survey", staticmethod(survey))

    await run_workflow(Survey, subject=repository)

    survey.assert_awaited_once_with()
    assert (await Repository.get(id=repository.id)).gist == "Widgets for every shelf."


async def test_survey_workspace_clones_the_subject_repo(druks_db, monkeypatch):
    workflow = Survey()
    workflow.subject = await Repository.create(repo="acme/widgets")
    workflow.account_id = None
    host = SimpleNamespace(id="h1", ssh_username="exedev")

    workspace = workflow.workspace_class(await workflow.subject)
    workspace.host = host

    assert (workspace.subject.repo, workspace.subject.ref) == ("acme/widgets", None)
    assert workspace.repo_path == get_repo_root("exedev")
    # The box's secret names the operator's vault row and the repo before the box exists.
    row = await connect_service(
        "github", identity={"app_id": "1", "slug": "druks-operator"}, secrets={"private_key": "pem"}
    )
    [secret] = await workflow.workspace_class.get_secrets(await workflow.subject)
    assert secret == SandboxSecret(
        name="github", secret_id=row.id, resources=("acme/widgets",), host="github.com"
    )
