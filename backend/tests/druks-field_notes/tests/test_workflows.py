from unittest import mock

from druks.testing import run_workflow
from druks_field_notes.contracts import GistOutput
from druks_field_notes.extension import FieldNotes
from druks_field_notes.models import Note
from druks_field_notes.workflows import Summarize


async def test_summarize_writes_the_gist(druks_db, monkeypatch):
    note = Note.create(body="the pump ran hot on the second pass")
    summarize = mock.AsyncMock(return_value=GistOutput(gist="The pump ran hot on the second pass."))
    monkeypatch.setattr(FieldNotes, "summarize", staticmethod(summarize))

    await run_workflow(Summarize, subject=note)

    summarize.assert_awaited_once_with(note_body=note.body)
    assert Note.get(note.id).gist == "The pump ran hot on the second pass."


async def test_dispatch_starts_the_workflow_for_the_note(monkeypatch):
    note = Note(id=42)
    start = mock.AsyncMock(return_value="run-1")
    monkeypatch.setattr(Summarize, "start", staticmethod(start))

    run_id = await Summarize.dispatch(note=note)

    assert run_id == "run-1"
    start.assert_awaited_once_with(subject=note)
