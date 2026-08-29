import httpx
import pytest
from druks_field_notes.models import Note


@pytest.fixture
async def note(druks_db) -> Note:
    return await Note.create(body="Fan noise on rack 3.")


async def test_the_roster_carries_the_page_table(druks_client: httpx.AsyncClient):
    roster = {entry["name"]: entry for entry in (await druks_client.get("/api/apps")).json()}

    pages = roster["field_notes"]["pages"]
    assert [page["name"] for page in pages] == [
        "notes",
        "new_note",
        "note",
        "note_history",
        "recent_notes",
    ]
    assert [page["path"] for page in pages] == [
        "/field_notes",
        "/field_notes/notes/new",
        "/field_notes/notes/{note_id}",
        "/field_notes/notes/{note_id}/history",
        "/field_notes/recent",
    ]
    by_name = {page["name"]: page for page in pages}
    assert by_name["note_history"]["parent"] == "note"
    assert by_name["note"]["parent"] == ""
    assert by_name["note_history"]["label"] == "note history"
    # Declaration order, so a tab strip reads the app's own order.
    assert by_name["recent_notes"]["order"] < by_name["new_note"]["order"]


async def test_the_landing_page_answers_at_the_bare_pages_path(druks_client: httpx.AsyncClient):
    response = await druks_client.get("/api/field_notes/pages")

    assert response.status_code == 200
    page = response.json()
    assert page["title"] == "Notes"
    assert page["blocks"][0]["block"] == "empty_state"
    assert page["blocks"][0]["actions"][0]["page"] == "new_note"


async def test_a_page_projects_the_app_data(druks_client: httpx.AsyncClient, note: Note):
    page = (await druks_client.get("/api/field_notes/pages")).json()

    (section,) = page["blocks"]
    assert section["block"] == "section"
    assert section["name"] == "recent"
    (card,) = section["blocks"]
    assert card["title"] == f"Note {note.id}"
    assert card["blocks"][0]["text"] == "Fan noise on rack 3."
    assert card["actions"][0] == {
        "block": "link",
        "label": "Open",
        "page": "note",
        "arguments": {"note_id": str(note.id)},
        "url": "",
    }


async def test_a_page_reads_its_route_parameter(druks_client: httpx.AsyncClient, note: Note):
    page = (await druks_client.get(f"/api/field_notes/pages/notes/{note.id}")).json()

    assert page["title"] == f"Note {note.id}"
    assert page["blocks"][0] == {"block": "markdown", "text": "Fan noise on rack 3."}


async def test_a_route_parameter_the_signature_rejects_answers_422(
    druks_client: httpx.AsyncClient,
):
    response = await druks_client.get("/api/field_notes/pages/notes/not-a-number")

    assert response.status_code == 422


async def test_a_literal_segment_answers_before_a_parameter(druks_client: httpx.AsyncClient):
    page = (await druks_client.get("/api/field_notes/pages/notes/new")).json()

    assert page["title"] == "Write a note"
    assert [block["block"] for block in page["blocks"]] == ["callout", "divider", "markdown"]


async def test_a_child_page_answers_under_its_parent(druks_client: httpx.AsyncClient, note: Note):
    response = await druks_client.get(f"/api/field_notes/pages/notes/{note.id}/history")

    assert response.status_code == 200
    assert response.json()["title"] == f"Note {note.id} history"


async def test_a_static_child_of_the_landing_page_answers(druks_client: httpx.AsyncClient):
    response = await druks_client.get("/api/field_notes/pages/recent")

    assert response.status_code == 200
    assert response.json()["title"] == "Recent notes"


async def test_an_undeclared_page_path_answers_404(druks_client: httpx.AsyncClient):
    response = await druks_client.get("/api/field_notes/pages/nowhere/at/all")

    assert response.status_code == 404
