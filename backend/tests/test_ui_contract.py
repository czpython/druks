import httpx
import pytest
from druks.apps.loader import load_app
from druks_field_notes.models import Note


async def failing_page(monkeypatch, project) -> httpx.AsyncClient:
    """A page router mounted after ``project`` replaced a page; the shared
    server captured the real functions."""
    from druks.accounts.dependencies import current_account
    from druks.api.server import _page_read_error_handler
    from druks.ui.exceptions import PageReadError
    from druks_field_notes import pages
    from fastapi import Depends, FastAPI

    monkeypatch.setattr(pages.note, "function", project)
    app = load_app("field_notes")
    api = FastAPI()
    api.add_exception_handler(PageReadError, _page_read_error_handler)
    api.dependency_overrides[current_account] = lambda: None
    api.include_router(
        app._get_page_routes(), prefix="/api/field_notes", dependencies=[Depends(current_account)]
    )
    return api


async def test_a_page_that_raises_says_which_page_and_nothing_more(monkeypatch):
    from druks.testing import asgi_client

    async def raising(note_id: int):
        raise RuntimeError("connection to postgres://secret@host failed")

    api = await failing_page(monkeypatch, raising)
    async with asgi_client(api) as client:
        response = await client.get("/api/field_notes/pages/notes/1")

    assert response.status_code == 500
    detail = response.json()["detail"]
    assert response.json()["error"] == "PAGE_FAILED"
    assert (
        detail
        == "app 'field_notes' page 'note' could not be read: its own code raised RuntimeError"
    )
    # Whatever the app's own code said stays in the log.
    assert "postgres" not in detail


async def test_a_page_that_answers_with_something_else_says_so(monkeypatch):
    from druks.testing import asgi_client

    async def not_a_page(note_id: int):
        return {"title": "Note"}

    api = await failing_page(monkeypatch, not_a_page)
    async with asgi_client(api) as client:
        response = await client.get("/api/field_notes/pages/notes/1")

    assert response.status_code == 500
    assert "not a Page" in response.json()["detail"]


async def test_a_page_naming_an_operation_the_app_lacks_says_which(monkeypatch):
    from druks.testing import asgi_client
    from druks.ui import Action, Page

    async def bad_action(note_id: int):
        return Page("Note", blocks=[Action(label="Go", operation="nowhere")])

    api = await failing_page(monkeypatch, bad_action)
    async with asgi_client(api) as client:
        response = await client.get("/api/field_notes/pages/notes/1")

    assert response.status_code == 500
    assert "nowhere" in response.json()["detail"]


async def test_every_page_answers_for_the_proof_app(druks_client: httpx.AsyncClient, druks_db):
    note = await Note.create(body="Fan noise on rack 3.")

    for path in [
        "",
        "/recent",
        "/notes/new",
        f"/notes/{note.id}",
        f"/notes/{note.id}/history",
    ]:
        response = await druks_client.get(f"/api/field_notes/pages{path}")
        assert response.status_code == 200, path
        assert response.json()["title"], path


@pytest.mark.parametrize(
    "path, says",
    [("/nowhere", 404), ("/notes/not-a-number", 422)],
)
async def test_a_page_read_that_cannot_resolve_says_which(
    druks_client: httpx.AsyncClient, path, says
):
    assert (await druks_client.get(f"/api/field_notes/pages{path}")).status_code == says


def test_the_test_plugin_claims_every_installed_app():
    from druks.apps.loader import _workflow_packages

    # An app's own suite imports its workflows module, and a Workflow resolves
    # its app at definition. Loading the plugin is what makes that work outside
    # this repository.
    assert _workflow_packages["druks_field_notes"] == "field_notes"
