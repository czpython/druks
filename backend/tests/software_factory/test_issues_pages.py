from druks.contrib.software_factory.issues.models import Ticket

BOARD_COLUMNS = [
    "Backlog",
    "Todo",
    "Ready for Agent",
    "In Progress",
    "In Review",
    "Done",
]
LIST_SECTIONS = [
    "In Progress",
    "In Review",
    "Ready for Agent",
    "Todo",
    "Backlog",
    "Done",
    "Cancelled",
]

_PAGES = "/api/software_factory/pages"
_TICKETS = "/api/software_factory/tickets"
_PROJECTS = "/api/software_factory/ticket-projects"


async def _open_project(druks_client, *, name="druks", prefix="dru"):
    created = await druks_client.post(_PROJECTS, json={"name": name, "prefix": prefix})
    assert created.status_code == 201
    return created.json()


async def _open_ticket(druks_client, project_id, **fields):
    created = await druks_client.post(
        _TICKETS,
        json={"title": "one", "project_id": project_id, **fields},
    )
    assert created.status_code == 201
    return created.json()


def _columns(page: dict) -> list[dict]:
    return page["blocks"][0]["blocks"]


def _cards_in(column: dict) -> list[dict]:
    return column["blocks"][0]["cards"]


def _tables(page: dict) -> list[dict]:
    return page["blocks"][0]["blocks"]


def _comments(page: dict) -> dict:
    left = page["blocks"][0]["blocks"][0]["blocks"]
    return next(block for block in left if block.get("name") == "comments")


async def test_empty_board_shows_columns_and_create_actions(druks_client):
    page = (await druks_client.get(f"{_PAGES}/board")).json()

    assert page["title"] == "Board"
    assert [control["label"] for control in page["controls"]] == ["New ticket", "New project"]
    assert [control["operation"] for control in page["controls"]] == [
        "create_ticket",
        "create_ticket_project",
    ]
    columns = _columns(page)
    assert [column["title"] for column in columns] == BOARD_COLUMNS
    for column in columns:
        cards = column["blocks"][0]
        assert cards["cards"] == []
        assert cards["empty"]["title"] == "Nothing here"


async def test_created_ticket_lands_in_todo_on_board_and_list(druks_client):
    project = await _open_project(druks_client)
    ticket = await _open_ticket(druks_client, project["id"], title="Ship the board")

    board = (await druks_client.get(f"{_PAGES}/board")).json()
    by_title = {column["title"]: column for column in _columns(board)}
    (card,) = _cards_in(by_title["Todo"])
    assert card["title"] == "Ship the board"
    assert card["description"].startswith("DRU-1")
    assert card["link"]["arguments"] == {"identifier": ticket["identifier"]}
    assert card["controls"] == []
    for title in BOARD_COLUMNS:
        if title != "Todo":
            assert _cards_in(by_title[title]) == []

    listed = (await druks_client.get(f"{_PAGES}/list")).json()
    by_section = {table["title"]: table for table in _tables(listed)}
    assert [table["title"] for table in _tables(listed)] == LIST_SECTIONS
    (row,) = by_section["Todo"]["rows"]
    assert row["cells"][0]["text"] == "DRU-1"
    assert row["cells"][1]["text"] == "Ship the board"
    assert row["cells"][1]["link"]["arguments"] == {"identifier": ticket["identifier"]}
    for title in LIST_SECTIONS:
        if title != "Todo":
            assert by_section[title]["rows"] == []


async def test_moving_a_ticket_updates_board_and_list(druks_client):
    project = await _open_project(druks_client)
    ticket = await _open_ticket(druks_client, project["id"], title="In flight")
    moved = await druks_client.post(
        f"{_TICKETS}/{ticket['identifier']}/status",
        json={"status": "in_progress"},
    )
    assert moved.status_code == 200

    board = (await druks_client.get(f"{_PAGES}/board")).json()
    by_title = {column["title"]: column for column in _columns(board)}
    assert [card["title"] for card in _cards_in(by_title["In Progress"])] == ["In flight"]
    assert _cards_in(by_title["Todo"]) == []

    listed = (await druks_client.get(f"{_PAGES}/list")).json()
    by_section = {table["title"]: table for table in _tables(listed)}
    assert [row["cells"][1]["text"] for row in by_section["In Progress"]["rows"]] == ["In flight"]
    assert by_section["Todo"]["rows"] == []


async def test_cancelled_tickets_are_off_the_board_and_last_on_the_list(druks_client):
    project = await _open_project(druks_client)
    await _open_ticket(druks_client, project["id"], title="live")
    gone = await _open_ticket(druks_client, project["id"], title="gone")
    await druks_client.post(
        f"{_TICKETS}/{gone['identifier']}/status",
        json={"status": "cancelled"},
    )

    board = (await druks_client.get(f"{_PAGES}/board")).json()
    cards = [card["title"] for column in _columns(board) for card in _cards_in(column)]
    assert cards == ["live"]

    listed = (await druks_client.get(f"{_PAGES}/list")).json()
    tables = _tables(listed)
    assert [table["title"] for table in tables] == LIST_SECTIONS
    assert tables[-1]["title"] == "Cancelled"
    assert [row["cells"][1]["text"] for row in tables[-1]["rows"]] == ["gone"]


async def test_ticket_page_follows_the_row_and_comments_refresh_the_region(druks_client):
    project = await _open_project(druks_client)
    created = await _open_ticket(druks_client, project["id"], title="Follow me")
    row = await Ticket.get_for_identifier(created["identifier"])

    page = (await druks_client.get(f"{_PAGES}/tickets/{created['identifier']}")).json()

    assert page["title"] == created["identifier"]
    assert page["follows"] == {"subjectType": "ticket", "subjectId": str(row.id)}
    assert page["controls"] == []
    columns = page["blocks"][0]
    assert columns["layout"] == "sidebar"
    left = columns["blocks"][0]["blocks"]
    prose = left[0]
    assert prose["submit"] == "change"
    assert prose["layout"] == "prose"
    assert prose["fields"][0]["value"] == "Follow me"
    assert prose["action"]["operation"] == "update_ticket"
    status = columns["blocks"][1]["blocks"][0]
    assert status["action"]["operation"] == "set_status"
    assert status["submit"] == "change"
    comments = _comments(page)
    assert comments["title"] == "Comments"
    assert comments["blocks"][0]["title"] == "No comments yet"
    comment_form = comments["blocks"][1]
    assert comment_form["action"]["operation"] == "add_comment"
    assert comment_form["action"]["refresh"] == "region"

    written = await druks_client.post(
        f"{_TICKETS}/{created['identifier']}/comments",
        json={"body": "looks good"},
    )
    assert written.status_code == 201

    after = (await druks_client.get(f"{_PAGES}/tickets/{created['identifier']}")).json()
    thread = _comments(after)
    assert thread["blocks"][0]["blocks"][0]["text"] == "looks good"


async def test_unknown_ticket_page_is_an_empty_state(druks_client):
    page = (await druks_client.get(f"{_PAGES}/tickets/NOPE-1")).json()

    assert page["blocks"][0]["title"] == "No such ticket"
    assert page["blocks"][0]["controls"][0]["page"] == "board"


async def test_roster_names_the_board_and_list_pages(druks_client):
    roster = {entry["name"]: entry for entry in (await druks_client.get("/api/apps")).json()}

    names = [page["name"] for page in roster["software_factory"]["pages"]]
    # Route-match order: the static list wins over the parameterized ticket.
    assert names == ["board", "list", "ticket"]
    assert roster["software_factory"]["navigation"] == []
