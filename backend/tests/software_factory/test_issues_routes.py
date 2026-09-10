from druks.accounts.models import Account
from druks.api.server import app as api
from druks.contrib.software_factory.issues.enums import Status

_TICKETS = "/api/software_factory/tickets"
_PROJECTS = "/api/software_factory/projects"


def _published(monkeypatch):
    events = []

    async def emit(name, **kwargs):
        events.append((name, kwargs["payload"]))

    monkeypatch.setattr("druks.contrib.software_factory.issues.models.publish", emit)
    return events


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


def test_get_and_comment_are_agent_operations():
    schema = api.openapi()
    get_ticket = schema["paths"][f"{_TICKETS}/{{identifier}}"]["get"]
    add_comment = schema["paths"][f"{_TICKETS}/{{identifier}}/comments"]["post"]
    assert "agent" in get_ticket["tags"]
    assert get_ticket["operationId"] in {"get_ticket", "software_factory_get_ticket"}
    assert "agent" in add_comment["tags"]
    assert add_comment["operationId"] in {"add_comment", "software_factory_add_comment"}


async def test_create_as_backlog_does_not_publish(druks_client, monkeypatch):
    events = _published(monkeypatch)
    repo = await _open_repo(druks_client)
    ticket = await _open_ticket(druks_client, repo["id"], title="quiet")

    assert ticket["identifier"] == "DRU-1"
    assert ticket["status"] == "backlog"
    assert ticket["comments"] == []
    assert events == []


async def test_create_as_ready_for_agent_publishes_the_trigger(druks_client, monkeypatch):
    events = _published(monkeypatch)
    repo = await _open_repo(druks_client, repo="acme/acme-app")
    ticket = await _open_ticket(druks_client, repo["id"], status="ready_for_agent", title="go")

    assert ticket["status"] == "ready_for_agent"
    assert events == [
        (
            "ticket.transitioned",
            {
                "source": "issues",
                "identifier": "DRU-1",
                "status": Status.READY_FOR_AGENT.label,
                "title": "go",
                "url": "/software_factory/tickets/DRU-1",
                "project_name": "acme-app",
                "labels": [],
                "assignee_email": None,
                "assignee_name": None,
                "completed": False,
                "terminal": False,
            },
        )
    ]


async def test_set_status_publishes_one_transition_with_display_labels(druks_client, monkeypatch):
    events = _published(monkeypatch)
    repo = await _open_repo(druks_client, repo="acme/acme-app")
    ticket = await _open_ticket(druks_client, repo["id"], title="Add an endpoint")

    moved = await druks_client.post(
        f"{_TICKETS}/{ticket['identifier']}/status",
        json={"status": "ready_for_agent"},
    )

    assert moved.status_code == 200
    assert moved.json()["status"] == "ready_for_agent"
    assert events == [
        (
            "ticket.transitioned",
            {
                "source": "issues",
                "identifier": "DRU-1",
                "status": Status.READY_FOR_AGENT.label,
                "title": "Add an endpoint",
                "url": "/software_factory/tickets/DRU-1",
                "project_name": "acme-app",
                "labels": [],
                "assignee_email": None,
                "assignee_name": None,
                "completed": False,
                "terminal": False,
            },
        )
    ]

    again = await druks_client.post(
        f"{_TICKETS}/{ticket['identifier']}/status",
        json={"status": "ready_for_agent"},
    )
    assert again.status_code == 200
    assert len(events) == 1


async def test_set_status_marks_done_completed_and_cancelled_terminal(druks_client, monkeypatch):
    events = _published(monkeypatch)
    repo = await _open_repo(druks_client)
    ticket = await _open_ticket(druks_client, repo["id"])

    done = await druks_client.post(
        f"{_TICKETS}/{ticket['identifier']}/status",
        json={"status": "done"},
    )
    cancelled = await druks_client.post(
        f"{_TICKETS}/{ticket['identifier']}/status",
        json={"status": "cancelled"},
    )

    assert done.status_code == 200
    assert cancelled.status_code == 200
    assert [payload["status"] for _, payload in events] == ["Done", "Cancelled"]
    assert [payload["completed"] for _, payload in events] == [True, False]
    assert [payload["terminal"] for _, payload in events] == [True, True]


async def test_update_ticket_never_publishes_and_cannot_set_status(druks_client, monkeypatch):
    events = _published(monkeypatch)
    repo = await _open_repo(druks_client)
    ticket = await _open_ticket(druks_client, repo["id"], title="old")

    edited = await druks_client.patch(
        f"{_TICKETS}/{ticket['identifier']}",
        json={"title": "new", "status": "done", "priority": "high"},
    )

    assert edited.status_code == 200
    body = edited.json()
    assert body["title"] == "new"
    assert body["priority"] == "high"
    assert body["status"] == "backlog"
    assert events == []


async def test_blank_owner_is_nobody(druks_client):
    repo = await _open_repo(druks_client)
    created = await druks_client.post(
        _TICKETS,
        json={"title": "unheld", "repo_id": int(repo["id"]), "owner_id": ""},
    )

    assert created.status_code == 201
    assert created.json()["owner_id"] is None

    ticket = created.json()
    assigned = await Account.get_or_create("dev@example.com")
    held = await druks_client.patch(
        f"{_TICKETS}/{ticket['identifier']}",
        json={"owner_id": assigned.id},
    )
    assert held.status_code == 200
    assert held.json()["owner_id"] == assigned.id

    cleared = await druks_client.patch(
        f"{_TICKETS}/{ticket['identifier']}",
        json={"owner_id": ""},
    )
    assert cleared.status_code == 200
    assert cleared.json()["owner_id"] is None


async def test_update_can_move_a_ticket_to_another_repo(druks_client):
    first = await _open_repo(druks_client, project="Alpha", prefix="alp", repo="acme/alpha")
    second = await _open_repo(druks_client, project="Beta", prefix="bet", repo="acme/beta")
    ticket = await _open_ticket(druks_client, first["id"])

    moved = await druks_client.patch(
        f"{_TICKETS}/{ticket['identifier']}",
        json={"repo_id": int(second["id"])},
    )

    assert moved.status_code == 200
    assert moved.json()["repo_id"] == int(second["id"])
    assert moved.json()["identifier"] == "ALP-1"


async def test_add_comment_authors_from_the_request_account(druks_client):
    account = await Account.get_or_create("op@example.com")
    repo = await _open_repo(druks_client)
    ticket = await _open_ticket(druks_client, repo["id"])

    written = await druks_client.post(
        f"{_TICKETS}/{ticket['identifier']}/comments",
        json={"body": "ship it"},
    )

    assert written.status_code == 201
    comment = written.json()
    assert comment["author"] == account.username
    assert comment["body"] == "ship it"

    detail = await druks_client.get(f"{_TICKETS}/{ticket['identifier']}")
    assert detail.status_code == 200
    assert [line["author"] for line in detail.json()["comments"]] == [account.username]


async def test_blank_title_and_body_are_refused(druks_client):
    repo = await _open_repo(druks_client)

    created = await druks_client.post(
        _TICKETS,
        json={"title": "   ", "repo_id": int(repo["id"])},
    )
    assert created.status_code == 422

    ticket = await _open_ticket(druks_client, repo["id"])
    edited = await druks_client.patch(
        f"{_TICKETS}/{ticket['identifier']}",
        json={"title": " "},
    )
    assert edited.status_code == 422

    commented = await druks_client.post(
        f"{_TICKETS}/{ticket['identifier']}/comments",
        json={"body": "\n"},
    )
    assert commented.status_code == 422


async def test_unknown_ticket_and_unknown_owner_are_404(druks_client):
    missing = await druks_client.get(f"{_TICKETS}/DRU-99")
    assert missing.status_code == 404

    repo = await _open_repo(druks_client)
    assigned = await druks_client.post(
        _TICKETS,
        json={
            "title": "handed to nobody real",
            "repo_id": int(repo["id"]),
            "owner_id": "not-an-account",
        },
    )
    assert assigned.status_code == 404

    ticket = await _open_ticket(druks_client, repo["id"])
    updated = await druks_client.patch(
        f"{_TICKETS}/{ticket['identifier']}",
        json={"owner_id": "not-an-account"},
    )
    assert updated.status_code == 404

    gone = await druks_client.post(f"{_TICKETS}/NOPE-1/status", json={"status": "done"})
    assert gone.status_code == 404
