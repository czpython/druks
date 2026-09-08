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
    "Backlog",
    "Todo",
    "Ready for Agent",
    "In Progress",
    "In Review",
    "Done",
    "Blocked",
    "Cancelled",
]

_PAGES = "/api/software_factory/pages"
_TICKETS = "/api/software_factory/tickets"
_PROJECTS = "/api/software_factory/projects"


async def _open_repo(druks_client, *, project="Acme", prefix="dru", repo="acme/druks"):
    created = await druks_client.post(_PROJECTS, json={"name": project, "prefix": prefix})
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


def _cards_in(column: dict) -> list[dict]:
    return column["blocks"][0]["cards"]


def _tables(page: dict) -> list[dict]:
    return page["blocks"][0]["blocks"]


def _section_name(title: str) -> str:
    return title[: title.rindex(" (")]


def _by_section(page: dict) -> dict[str, dict]:
    return {_section_name(table["title"]): table for table in _tables(page)}


def _section_titles(counts: dict[str, int]) -> list[str]:
    return [f"{label} ({counts.get(label, 0)})" for label in LIST_SECTIONS]


def _comments(page: dict) -> dict:
    left = page["blocks"][0]["blocks"][0]["blocks"]
    return next(block for block in left if block.get("name") == "comments")


async def test_empty_board_shows_columns_and_create_actions(druks_client):
    page = (await druks_client.get(f"{_PAGES}/board")).json()

    assert page["title"] == "Board"
    assert page["description"] == ""
    assert [field["name"] for field in page["filters"]] == [
        "status",
        "priority",
        "updated",
        "assignee",
        "creator",
        "project",
        "repo",
    ]
    assert [control["label"] for control in page["controls"]] == ["New ticket"]
    assert [control["operation"] for control in page["controls"]] == ["create_ticket"]
    assert page["controls"][0]["fields"][1]["name"] == "repo_id"
    columns = _columns(page)
    assert [column["title"] for column in columns] == BOARD_COLUMNS
    for column in columns:
        cards = column["blocks"][0]
        assert cards["cards"] == []
        assert cards["empty"]["title"] == "Nothing here"


async def test_created_ticket_lands_in_todo_on_board_and_issues(druks_client):
    repo = await _open_repo(druks_client)
    ticket = await _open_ticket(druks_client, repo["id"], title="Ship the board")

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

    listed = (await druks_client.get(f"{_PAGES}/issues")).json()
    by_section = _by_section(listed)
    assert listed["title"] == "Issues"
    assert listed["description"] == ""
    assert [table["title"] for table in _tables(listed)] == _section_titles({"Todo": 1})
    (row,) = by_section["Todo"]["rows"]
    assert row["cells"][0]["text"] == "DRU-1"
    assert row["cells"][1]["text"] == "Ship the board"
    assert row["cells"][1]["link"]["arguments"] == {"identifier": ticket["identifier"]}
    assert row["cells"][4]["text"] == "acme/druks"
    for title in LIST_SECTIONS:
        if title != "Todo":
            assert by_section[title]["rows"] == []


async def test_moving_a_ticket_updates_board_and_issues(druks_client):
    repo = await _open_repo(druks_client)
    ticket = await _open_ticket(druks_client, repo["id"], title="In flight")
    moved = await druks_client.post(
        f"{_TICKETS}/{ticket['identifier']}/status",
        json={"status": "in_progress"},
    )
    assert moved.status_code == 200

    board = (await druks_client.get(f"{_PAGES}/board")).json()
    by_title = {column["title"]: column for column in _columns(board)}
    assert [card["title"] for card in _cards_in(by_title["In Progress"])] == ["In flight"]
    assert _cards_in(by_title["Todo"]) == []

    listed = (await druks_client.get(f"{_PAGES}/issues")).json()
    by_section = _by_section(listed)
    assert [row["cells"][1]["text"] for row in by_section["In Progress"]["rows"]] == ["In flight"]
    assert by_section["Todo"]["rows"] == []


async def test_cancelled_tickets_are_off_the_board_and_last_on_issues(druks_client):
    repo = await _open_repo(druks_client)
    await _open_ticket(druks_client, repo["id"], title="live")
    gone = await _open_ticket(druks_client, repo["id"], title="gone")
    await druks_client.post(
        f"{_TICKETS}/{gone['identifier']}/status",
        json={"status": "cancelled"},
    )

    board = (await druks_client.get(f"{_PAGES}/board")).json()
    cards = [card["title"] for column in _columns(board) for card in _cards_in(column)]
    assert cards == ["live"]

    listed = (await druks_client.get(f"{_PAGES}/issues")).json()
    tables = _tables(listed)
    assert [table["title"] for table in tables] == _section_titles({"Todo": 1, "Cancelled": 1})
    assert _section_name(tables[-1]["title"]) == "Cancelled"
    assert [row["cells"][1]["text"] for row in tables[-1]["rows"]] == ["gone"]


async def test_ticket_page_follows_the_row_and_comments_refresh_the_region(druks_client):
    repo = await _open_repo(druks_client)
    created = await _open_ticket(druks_client, repo["id"], title="Follow me")
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
    repo = columns["blocks"][1]["blocks"][3]
    assert repo["fields"][0]["name"] == "repo_id"
    assert repo["fields"][0]["options"][0]["group"] == "Acme"
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


async def test_new_ticket_groups_repos_by_github_project(druks_client):
    acme = (await druks_client.post(_PROJECTS, json={"name": "Acme", "prefix": "acm"})).json()
    one = (
        await druks_client.post(f"{_PROJECTS}/{acme['id']}/repos", json={"fullName": "acme/one"})
    ).json()
    two = (
        await druks_client.post(f"{_PROJECTS}/{acme['id']}/repos", json={"fullName": "acme/two"})
    ).json()
    beta = await _open_repo(druks_client, project="Beta", prefix="bet", repo="beta/app")

    board = (await druks_client.get(f"{_PAGES}/board")).json()
    repo_field = next(
        field for field in board["controls"][0]["fields"] if field["name"] == "repo_id"
    )
    assert [option["group"] for option in repo_field["options"]] == ["Acme", "Acme", "Beta"]
    assert [option["label"] for option in repo_field["options"]] == [
        "acme/one",
        "acme/two",
        "beta/app",
    ]
    assert [option["value"] for option in repo_field["options"]] == [
        str(one["id"]),
        str(two["id"]),
        str(beta["id"]),
    ]
    assert repo_field["value"] == str(one["id"])


async def test_unknown_ticket_page_is_an_empty_state(druks_client):
    page = (await druks_client.get(f"{_PAGES}/tickets/NOPE-1")).json()

    assert page["blocks"][0]["title"] == "No such ticket"
    assert page["blocks"][0]["controls"][0]["page"] == "board"


async def test_roster_names_the_board_and_issues_pages(druks_client):
    roster = {entry["name"]: entry for entry in (await druks_client.get("/api/apps")).json()}

    names = [page["name"] for page in roster["software_factory"]["pages"]]
    # Route-match order: the static issues page wins over the parameterized ticket.
    assert names == ["board", "issues", "ticket"]
    assert roster["software_factory"]["navigation"] == []


async def test_board_status_filter_keeps_columns_and_shows_cancelled_when_asked(druks_client):
    repo = await _open_repo(druks_client)
    await _open_ticket(druks_client, repo["id"], title="live")
    gone = await _open_ticket(druks_client, repo["id"], title="gone")
    await druks_client.post(
        f"{_TICKETS}/{gone['identifier']}/status",
        json={"status": "cancelled"},
    )

    todo = (await druks_client.get(f"{_PAGES}/board", params={"status": "todo"})).json()
    cards = [card["title"] for column in _columns(todo) for card in _cards_in(column)]
    assert cards == ["live"]
    assert [column["title"] for column in _columns(todo)] == BOARD_COLUMNS

    cancelled = (await druks_client.get(f"{_PAGES}/board", params={"status": "cancelled"})).json()
    assert [column["title"] for column in _columns(cancelled)] == [*BOARD_COLUMNS, "Cancelled"]
    cards = [card["title"] for column in _columns(cancelled) for card in _cards_in(column)]
    assert cards == ["gone"]


async def test_board_filters_by_repo_assignee_and_creator(druks_client):
    acme = (await druks_client.post(_PROJECTS, json={"name": "Acme", "prefix": "acm"})).json()
    acme_repo = (
        await druks_client.post(f"{_PROJECTS}/{acme['id']}/repos", json={"fullName": "acme/one"})
    ).json()
    beta_repo = await _open_repo(druks_client, project="Beta", prefix="bet", repo="beta/app")
    me = (await druks_client.get("/api/auth/me")).json()["account"]["id"]
    await _open_ticket(druks_client, acme_repo["id"], title="assigned", assignee_id=me)
    await _open_ticket(druks_client, acme_repo["id"], title="open")
    await _open_ticket(druks_client, beta_repo["id"], title="elsewhere")

    by_repo = (await druks_client.get(f"{_PAGES}/board", params={"repo": beta_repo["id"]})).json()
    assert [card["title"] for column in _columns(by_repo) for card in _cards_in(column)] == [
        "elsewhere"
    ]

    unassigned = (await druks_client.get(f"{_PAGES}/board", params={"assignee": "none"})).json()
    assert {card["title"] for column in _columns(unassigned) for card in _cards_in(column)} == {
        "elsewhere",
        "open",
    }

    mine = (await druks_client.get(f"{_PAGES}/board", params={"creator": me})).json()
    titles = [card["title"] for column in _columns(mine) for card in _cards_in(column)]
    assert set(titles) == {"assigned", "open", "elsewhere"}

    issues = (await druks_client.get(f"{_PAGES}/issues", params={"project": acme["id"]})).json()
    titles = [row["cells"][1]["text"] for table in _tables(issues) for row in table["rows"]]
    assert set(titles) == {"assigned", "open"}
