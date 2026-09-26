from druks_field_notes.models import Note, Repository
from druks_field_notes.workflows import Summarize


async def test_notes_routes_create_and_list_notes(druks_client, monkeypatch):
    # The route dispatches from its own session and that session closes with the
    # request, so read the note's id here while the row is still attached.
    summarized = []

    async def dispatch(*, note):
        summarized.append(note.id)
        return "run-1"

    monkeypatch.setattr(Summarize, "dispatch", staticmethod(dispatch))

    created = await druks_client.post(
        "/api/field_notes/notes",
        json={"body": "the pump ran hot"},
    )

    assert created.status_code == 201
    note = await Note.get_for_id(created.json()["id"])
    assert note.body == "the pump ran hot"
    assert summarized == [note.id]

    listed = await druks_client.get("/api/field_notes/notes")

    assert listed.status_code == 200
    assert listed.json()[0]["body"] == "the pump ran hot"
    assert listed.json()[0]["gist"] is None


async def test_repository_board_and_detail_read_the_subject(druks_client):
    repository = await Repository.create(repo="acme/widgets")
    newer = await Repository.create(repo="acme/gadgets")

    board = await druks_client.get("/api/field_notes/repository")
    detail = await druks_client.get(f"/api/field_notes/repository/{repository.id}")

    assert board.status_code == 200
    assert [row["summary"]["repo"] for row in board.json()["rows"]] == [newer.repo, repository.repo]
    assert detail.status_code == 200
    assert detail.json()["summary"] == {
        "id": str(repository.id),
        "key": "acme/widgets",
        "title": None,
        "repo": "acme/widgets",
        "gist": None,
    }


async def test_a_missing_note_is_a_404_on_a_route_and_an_empty_state_on_a_page(druks_client):
    route = await druks_client.post("/api/field_notes/notes/999/gist")
    page = await druks_client.get("/api/field_notes/pages/notes/999")

    assert route.status_code == 404
    assert route.json() == {"error": "HTTP_404", "detail": "No note 999"}
    assert page.status_code == 200
    assert page.json()["title"] == "No note 999"
    assert page.json()["blocks"][0]["block"] == "empty_state"
