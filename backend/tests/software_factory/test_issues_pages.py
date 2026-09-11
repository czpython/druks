from druks.accounts.models import Account
from druks.contrib.software_factory.models import ProjectRepo, Ticket

from software_factory.factories import make_test_work_item

BOARD_COLUMNS = [
    "Backlog",
    "Ready for Agent",
    "In Progress",
    "Blocked",
    "In Review",
    "Done",
]

_PAGES = "/api/software_factory/pages"
_TICKETS = "/api/software_factory/tickets"
_PROJECTS = "/api/software_factory/projects"


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


def _cards_in(column: dict) -> list[dict]:
    return column["blocks"][0]["cards"]


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
        "owner",
        "creator",
        "project",
        "repo",
    ]
    assert [control["label"] for control in page["controls"]] == ["New ticket"]
    assert [control["operation"] for control in page["controls"]] == ["create_ticket"]
    assert page["controls"][0]["fields"][1]["name"] == "repo_id"
    me = (await druks_client.get("/api/auth/me")).json()["account"]["id"]
    owner = next(field for field in page["controls"][0]["fields"] if field["name"] == "owner_id")
    assert owner["label"] == "Owner"
    assert owner["value"] == me
    assert owner["options"][0]["label"] == "Unowned"
    columns = _columns(page)
    assert [column["title"] for column in columns] == BOARD_COLUMNS
    for column in columns:
        cards = column["blocks"][0]
        assert cards["layout"] == "stack"
        assert cards["drop"]["operation"] == "set_status"
        assert cards["drop"]["refresh"] == "page"
        assert cards["cards"] == []
        assert cards["empty"]["title"] == "Nothing here"
    assert [column["blocks"][0]["drop"]["arguments"]["status"] for column in columns] == [
        "backlog",
        "ready_for_agent",
        "in_progress",
        "blocked",
        "in_review",
        "done",
    ]
    status_filter = next(field for field in page["filters"] if field["name"] == "status")
    assert [option["label"] for option in status_filter["options"]] == ["Any", *BOARD_COLUMNS]
    create_status = next(
        field for field in page["controls"][0]["fields"] if field["name"] == "status"
    )
    assert [option["label"] for option in create_status["options"]] == BOARD_COLUMNS


async def test_created_ticket_lands_in_backlog_on_the_board(druks_client):
    repo = await _open_repo(druks_client)
    ticket = await _open_ticket(druks_client, repo["id"], title="Ship the board")

    board = (await druks_client.get(f"{_PAGES}/board")).json()
    by_title = {column["title"]: column for column in _columns(board)}
    (card,) = _cards_in(by_title["Backlog"])
    assert card["title"] == "Ship the board"
    assert card["description"].startswith("ACM-1")
    assert card["link"]["arguments"] == {"identifier": ticket["identifier"]}
    assert card["drag"] == {"identifier": ticket["identifier"]}
    assert card["controls"] == []
    for title in BOARD_COLUMNS:
        if title != "Backlog":
            assert _cards_in(by_title[title]) == []


async def test_moving_a_ticket_updates_the_board(druks_client):
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
    assert _cards_in(by_title["Backlog"]) == []


async def test_blocked_tickets_stay_on_the_board(druks_client):
    repo = await _open_repo(druks_client)
    await _open_ticket(druks_client, repo["id"], title="live")
    stuck = await _open_ticket(druks_client, repo["id"], title="stuck")
    moved = await druks_client.post(
        f"{_TICKETS}/{stuck['identifier']}/status",
        json={"status": "blocked"},
    )
    assert moved.status_code == 200

    board = (await druks_client.get(f"{_PAGES}/board")).json()
    by_title = {column["title"]: column for column in _columns(board)}
    assert [card["title"] for card in _cards_in(by_title["Blocked"])] == ["stuck"]
    assert [card["title"] for card in _cards_in(by_title["Backlog"])] == ["live"]


async def test_ticket_page_saves_in_place_and_comments_refresh_the_region(druks_client):
    repo = await _open_repo(druks_client)
    created = await _open_ticket(druks_client, repo["id"], title="Follow me")

    page = (await druks_client.get(f"{_PAGES}/tickets/{created['identifier']}")).json()

    assert page["title"] == created["identifier"]
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
    owner = columns["blocks"][1]["blocks"][2]
    assert owner["fields"][0]["name"] == "owner_id"
    assert owner["fields"][0]["label"] == "Owner"
    repo = columns["blocks"][1]["blocks"][3]
    assert repo["fields"][0]["name"] == "repo_id"
    assert repo["fields"][0]["options"][0]["group"] == "Acme"
    facts = columns["blocks"][1]["blocks"][-1]
    assert [fact["label"] for fact in facts["facts"]] == [
        "Identifier",
        "Created by",
        "Created",
        "Updated",
    ]
    assert facts["facts"][0]["value"]["text"] == created["identifier"]
    account = await Account.get_or_create("op@example.com")
    assert facts["facts"][1]["value"]["text"] == account.username
    assert facts["facts"][2]["value"]["value"] == "time"
    assert facts["facts"][3]["value"]["value"] == "time"
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


async def test_ticket_page_unattributed_creator_when_none_is_stored(druks_client):
    repo = await _open_repo(druks_client)
    ticket = await Ticket.create(repo=await ProjectRepo.get(int(repo["id"])), title="ghost")

    page = (await druks_client.get(f"{_PAGES}/tickets/{ticket.identifier}")).json()

    facts = page["blocks"][0]["blocks"][1]["blocks"][-1]
    created_by = next(fact for fact in facts["facts"] if fact["label"] == "Created by")
    assert created_by["value"]["text"] == "Unattributed"


async def test_ticket_page_links_the_open_build(druks_client):
    repo = await _open_repo(druks_client)
    created = await _open_ticket(druks_client, repo["id"], title="Follow me")
    item = await make_test_work_item(
        repo="acme/druks",
        source="issues",
        ticket_key=created["identifier"],
        title="Follow me",
    )

    page = (await druks_client.get(f"{_PAGES}/tickets/{created['identifier']}")).json()

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


async def test_new_ticket_groups_repos_by_github_project(druks_client):
    acme = (await druks_client.post(_PROJECTS, json={"name": "Acme"})).json()
    one = (
        await druks_client.post(f"{_PROJECTS}/{acme['id']}/repos", json={"fullName": "acme/one"})
    ).json()
    two = (
        await druks_client.post(f"{_PROJECTS}/{acme['id']}/repos", json={"fullName": "acme/two"})
    ).json()
    beta = await _open_repo(druks_client, project="Beta", repo="beta/app")

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


async def test_unknown_ticket_page_is_an_empty_state(druks_client):
    page = (await druks_client.get(f"{_PAGES}/tickets/NOPE-1")).json()

    assert page["blocks"][0]["title"] == "No such ticket"
    assert page["blocks"][0]["controls"][0]["page"] == "board"


async def test_roster_names_the_board_and_ticket_pages(druks_client):
    roster = {entry["name"]: entry for entry in (await druks_client.get("/api/apps")).json()}

    names = [page["name"] for page in roster["software_factory"]["pages"]]
    assert names == ["board", "ticket"]
    assert roster["software_factory"]["navigation"] == []


async def test_board_status_filter_keeps_columns(druks_client):
    repo = await _open_repo(druks_client)
    await _open_ticket(druks_client, repo["id"], title="live")
    stuck = await _open_ticket(druks_client, repo["id"], title="stuck")
    await druks_client.post(
        f"{_TICKETS}/{stuck['identifier']}/status",
        json={"status": "blocked"},
    )

    backlog = (await druks_client.get(f"{_PAGES}/board", params={"status": "backlog"})).json()
    cards = [card["title"] for column in _columns(backlog) for card in _cards_in(column)]
    assert cards == ["live"]
    assert [column["title"] for column in _columns(backlog)] == BOARD_COLUMNS

    blocked = (await druks_client.get(f"{_PAGES}/board", params={"status": "blocked"})).json()
    assert [column["title"] for column in _columns(blocked)] == BOARD_COLUMNS
    cards = [card["title"] for column in _columns(blocked) for card in _cards_in(column)]
    assert cards == ["stuck"]


async def test_board_filters_by_repo_owner_and_creator(druks_client):
    acme = (await druks_client.post(_PROJECTS, json={"name": "Acme"})).json()
    acme_repo = (
        await druks_client.post(f"{_PROJECTS}/{acme['id']}/repos", json={"fullName": "acme/one"})
    ).json()
    beta_repo = await _open_repo(druks_client, project="Beta", repo="beta/app")
    me = (await druks_client.get("/api/auth/me")).json()["account"]["id"]
    await _open_ticket(druks_client, acme_repo["id"], title="assigned", owner_id=me)
    await _open_ticket(druks_client, acme_repo["id"], title="open")
    await _open_ticket(druks_client, beta_repo["id"], title="elsewhere")

    by_repo = (await druks_client.get(f"{_PAGES}/board", params={"repo": beta_repo["id"]})).json()
    assert [card["title"] for column in _columns(by_repo) for card in _cards_in(column)] == [
        "elsewhere"
    ]

    unowned = (await druks_client.get(f"{_PAGES}/board", params={"owner": "none"})).json()
    assert {card["title"] for column in _columns(unowned) for card in _cards_in(column)} == {
        "elsewhere",
        "open",
    }

    mine = (await druks_client.get(f"{_PAGES}/board", params={"creator": me})).json()
    titles = [card["title"] for column in _columns(mine) for card in _cards_in(column)]
    assert set(titles) == {"assigned", "open", "elsewhere"}

    by_project = (await druks_client.get(f"{_PAGES}/board", params={"project": acme["id"]})).json()
    titles = [card["title"] for column in _columns(by_project) for card in _cards_in(column)]
    assert set(titles) == {"assigned", "open"}
