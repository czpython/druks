from druks.accounts.constants import SYSTEM_ACCOUNT_ID
from druks.accounts.models import Account
from druks.api.server import app as api
from druks.contrib.issues.enums import Status


def _published(monkeypatch):
    events = []

    async def emit(name, **kwargs):
        events.append((name, kwargs["payload"]))

    monkeypatch.setattr("druks.contrib.issues.routes.publish", emit)
    return events


async def _open_project(druks_client, *, name="druks", prefix="dru"):
    created = await druks_client.post("/api/issues/projects", json={"name": name, "prefix": prefix})
    assert created.status_code == 201
    return created.json()


async def _open_ticket(druks_client, project_id, **fields):
    created = await druks_client.post(
        "/api/issues/tickets",
        json={"title": "one", "project_id": project_id, **fields},
    )
    assert created.status_code == 201
    return created.json()


def test_get_and_comment_are_agent_operations():
    schema = api.openapi()
    get_ticket = schema["paths"]["/api/issues/tickets/{identifier}"]["get"]
    add_comment = schema["paths"]["/api/issues/tickets/{identifier}/comments"]["post"]
    assert "agent" in get_ticket["tags"]
    assert get_ticket["operationId"] == "issues_get_ticket"
    assert "agent" in add_comment["tags"]
    assert add_comment["operationId"] == "issues_add_comment"


async def test_create_does_not_publish(druks_client, monkeypatch):
    events = _published(monkeypatch)
    project = await _open_project(druks_client)
    ticket = await _open_ticket(
        druks_client, project["id"], status="ready_for_agent", title="quiet"
    )

    assert ticket["identifier"] == "DRU-1"
    assert ticket["status"] == "ready_for_agent"
    assert ticket["comments"] == []
    assert events == []


async def test_set_status_publishes_one_transition_with_display_labels(druks_client, monkeypatch):
    events = _published(monkeypatch)
    project = await _open_project(druks_client, name="acme-app")
    ticket = await _open_ticket(druks_client, project["id"], title="Add an endpoint")

    moved = await druks_client.post(
        f"/api/issues/tickets/{ticket['identifier']}/status",
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
                "url": "/issues/tickets/DRU-1",
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
        f"/api/issues/tickets/{ticket['identifier']}/status",
        json={"status": "ready_for_agent"},
    )
    assert again.status_code == 200
    assert len(events) == 1


async def test_set_status_marks_done_completed_and_cancelled_terminal(druks_client, monkeypatch):
    events = _published(monkeypatch)
    project = await _open_project(druks_client)
    ticket = await _open_ticket(druks_client, project["id"])

    done = await druks_client.post(
        f"/api/issues/tickets/{ticket['identifier']}/status",
        json={"status": "done"},
    )
    cancelled = await druks_client.post(
        f"/api/issues/tickets/{ticket['identifier']}/status",
        json={"status": "cancelled"},
    )

    assert done.status_code == 200
    assert cancelled.status_code == 200
    assert [payload["status"] for _, payload in events] == ["Done", "Cancelled"]
    assert [payload["completed"] for _, payload in events] == [True, False]
    assert [payload["terminal"] for _, payload in events] == [True, True]


async def test_update_ticket_never_publishes_and_cannot_set_status(druks_client, monkeypatch):
    events = _published(monkeypatch)
    project = await _open_project(druks_client)
    ticket = await _open_ticket(druks_client, project["id"], title="old")

    edited = await druks_client.patch(
        f"/api/issues/tickets/{ticket['identifier']}",
        json={"title": "new", "status": "done", "priority": "high"},
    )

    assert edited.status_code == 200
    body = edited.json()
    assert body["title"] == "new"
    assert body["priority"] == "high"
    assert body["status"] == "todo"
    assert events == []


async def test_add_comment_authors_from_the_request_account(druks_client):
    account = await Account.get_or_create("op@example.com")
    project = await _open_project(druks_client)
    ticket = await _open_ticket(druks_client, project["id"])

    written = await druks_client.post(
        f"/api/issues/tickets/{ticket['identifier']}/comments",
        json={"body": "ship it"},
    )

    assert written.status_code == 201
    comment = written.json()
    assert comment["author"] == account.username
    assert comment["body"] == "ship it"

    detail = await druks_client.get(f"/api/issues/tickets/{ticket['identifier']}")
    assert detail.status_code == 200
    assert [line["author"] for line in detail.json()["comments"]] == [account.username]


async def test_blank_title_and_body_are_refused(druks_client):
    project = await _open_project(druks_client)

    created = await druks_client.post(
        "/api/issues/tickets",
        json={"title": "   ", "project_id": project["id"]},
    )
    assert created.status_code == 422

    ticket = await _open_ticket(druks_client, project["id"])
    edited = await druks_client.patch(
        f"/api/issues/tickets/{ticket['identifier']}",
        json={"title": " "},
    )
    assert edited.status_code == 422

    commented = await druks_client.post(
        f"/api/issues/tickets/{ticket['identifier']}/comments",
        json={"body": "\n"},
    )
    assert commented.status_code == 422


async def test_unknown_ticket_and_system_assignee_are_404(druks_client):
    missing = await druks_client.get("/api/issues/tickets/DRU-99")
    assert missing.status_code == 404

    project = await _open_project(druks_client)
    assigned = await druks_client.post(
        "/api/issues/tickets",
        json={
            "title": "handed to the system",
            "project_id": project["id"],
            "assignee_id": SYSTEM_ACCOUNT_ID,
        },
    )
    assert assigned.status_code == 404

    ticket = await _open_ticket(druks_client, project["id"])
    updated = await druks_client.patch(
        f"/api/issues/tickets/{ticket['identifier']}",
        json={"assignee_id": SYSTEM_ACCOUNT_ID},
    )
    assert updated.status_code == 404

    gone = await druks_client.post("/api/issues/tickets/NOPE-1/status", json={"status": "done"})
    assert gone.status_code == 404
