from druks.contrib.software_factory.enums import Status
from druks.contrib.software_factory.models import ProjectRepo, Ticket

from software_factory.factories import make_test_work_item

_PAGES = "/api/software_factory/pages"
_TICKETS = "/api/software_factory/tickets"
_PROJECTS = "/api/software_factory/projects"
COLUMNS = [status.label for status in Status]


async def _open_repo(druks_client, *, project="Acme", repo="acme/druks"):
    created = await druks_client.post(_PROJECTS, json={"name": project})
    assert created.status_code == 201
    added = await druks_client.post(
        f"{_PROJECTS}/{created.json()['id']}/repos",
        json={"fullName": repo},
    )
    assert added.status_code == 201
    return added.json()


async def _open_ticket(druks_client, repo_id, **fields):
    created = await druks_client.post(
        _TICKETS,
        json={"title": "one", "repo_id": int(repo_id), **fields},
    )
    assert created.status_code == 201
    return created.json()


def _columns(page: dict) -> list[dict]:
    return page["blocks"][0]["blocks"]


def _cards(column: dict) -> list[dict]:
    return column["blocks"][0]["cards"]


async def test_an_empty_board_shows_its_columns_and_what_creates_a_ticket(druks_client):
    acme = (await druks_client.post(_PROJECTS, json={"name": "Acme"})).json()
    one = (
        await druks_client.post(f"{_PROJECTS}/{acme['id']}/repos", json={"fullName": "acme/one"})
    ).json()
    await _open_repo(druks_client, project="Beta", repo="beta/app")

    page = (await druks_client.get(f"{_PAGES}/board")).json()

    assert page["title"] == "Board"
    columns = _columns(page)
    assert [column["title"] for column in columns] == COLUMNS
    for column, status in zip(columns, Status, strict=True):
        cards = column["blocks"][0]
        assert cards["layout"] == "stack"
        assert cards["drop"]["operation"] == "set_status"
        assert cards["drop"]["arguments"] == {"status": status.value}
        assert cards["cards"] == []
    create = page["controls"][0]
    assert (create["label"], create["operation"]) == ("New ticket", "create_ticket")
    repos = next(field for field in create["fields"] if field["name"] == "repo_id")
    assert [(option["group"], option["label"]) for option in repos["options"]] == [
        ("Acme", "acme/one"),
        ("Beta", "beta/app"),
    ]
    assert repos["options"][0]["value"] == str(one["id"])
    me = (await druks_client.get("/api/auth/me")).json()["account"]["id"]
    owner = next(field for field in create["fields"] if field["name"] == "owner_id")
    assert owner["value"] == me


async def test_a_new_ticket_shows_on_the_board_as_a_card_that_opens_and_drags(druks_client):
    repo = await _open_repo(druks_client)
    ticket = await _open_ticket(druks_client, repo["id"], title="Ship the board")

    board = (await druks_client.get(f"{_PAGES}/board")).json()

    by_title = {column["title"]: column for column in _columns(board)}
    (card,) = _cards(by_title["Backlog"])
    assert card["title"] == "Ship the board"
    assert card["description"].startswith("ACM-1")
    assert card["link"]["arguments"] == {"identifier": ticket["identifier"]}
    assert card["drag"] == {"identifier": ticket["identifier"]}
    assert card["controls"] == []
    assert [title for title in COLUMNS if _cards(by_title[title])] == ["Backlog"]


async def test_a_filter_narrows_the_cards_and_keeps_the_columns(druks_client):
    repo = await _open_repo(druks_client)
    await _open_ticket(druks_client, repo["id"], title="live")
    stuck = await _open_ticket(druks_client, repo["id"], title="stuck")
    await druks_client.post(
        f"{_TICKETS}/{stuck['identifier']}/status",
        json={"status": "blocked"},
    )

    page = (await druks_client.get(f"{_PAGES}/board", params={"status": "blocked"})).json()

    assert [field["name"] for field in page["filters"]] == [
        "status",
        "priority",
        "updated",
        "owner",
        "creator",
        "project",
        "repo",
    ]
    assert [column["title"] for column in _columns(page)] == COLUMNS
    assert [card["title"] for column in _columns(page) for card in _cards(column)] == ["stuck"]


async def test_the_ticket_page_saves_in_place_and_opens_its_build(druks_client):
    repo = await _open_repo(druks_client)
    created = await _open_ticket(druks_client, repo["id"], title="Follow me")
    item = await make_test_work_item(
        repo="acme/druks",
        source="druks",
        ticket_key=created["identifier"],
        title="Follow me",
    )

    page = (await druks_client.get(f"{_PAGES}/tickets/{created['identifier']}")).json()

    assert page["title"] == created["identifier"]
    assert page["controls"] == [
        {
            "block": "link",
            "label": "Open build",
            "page": "",
            "arguments": {},
            "url": "",
            "subject": {"subjectType": "work_item", "subjectId": str(item.id)},
        }
    ]
    columns = page["blocks"][0]
    assert columns["layout"] == "sidebar"
    prose, comments = columns["blocks"][0]["blocks"]
    assert (prose["submit"], prose["layout"]) == ("change", "prose")
    assert prose["fields"][0]["value"] == "Follow me"
    assert prose["action"]["operation"] == "update_ticket"
    sidebar = columns["blocks"][1]["blocks"]
    assert [form["fields"][0]["name"] for form in sidebar[:-1]] == [
        "status",
        "priority",
        "owner_id",
        "repo_id",
    ]
    assert all(form["submit"] == "change" for form in sidebar[:-1])
    assert [fact["label"] for fact in sidebar[-1]["facts"]] == [
        "Identifier",
        "Created by",
        "Created",
        "Updated",
    ]
    assert comments["name"] == "comments"
    assert comments["blocks"][-1]["action"]["refresh"] == "region"

    written = await druks_client.post(
        f"{_TICKETS}/{created['identifier']}/comments",
        json={"body": "looks good"},
    )
    assert written.status_code == 201

    after = (await druks_client.get(f"{_PAGES}/tickets/{created['identifier']}")).json()
    thread = after["blocks"][0]["blocks"][0]["blocks"][1]
    assert thread["blocks"][0]["blocks"][0]["text"] == "looks good"


async def test_a_ticket_nobody_wrote_is_an_empty_state(druks_client):
    page = (await druks_client.get(f"{_PAGES}/tickets/NOPE-1")).json()

    assert page["blocks"][0]["title"] == "No such ticket"
    assert page["blocks"][0]["controls"][0]["page"] == "board"


async def test_an_unattributed_ticket_still_reads(druks_client):
    repo = await _open_repo(druks_client)
    ticket = await Ticket.create(repo=await ProjectRepo.get(int(repo["id"])), title="ghost")

    page = (await druks_client.get(f"{_PAGES}/tickets/{ticket.identifier}")).json()

    facts = page["blocks"][0]["blocks"][1]["blocks"][-1]["facts"]
    created_by = next(fact for fact in facts if fact["label"] == "Created by")
    assert created_by["value"]["text"] == "Unattributed"
