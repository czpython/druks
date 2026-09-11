from druks.accounts.models import Account
from druks.contrib.software_factory.issues.enums import Status

_TICKETS = "/api/software_factory/tickets"
_PROJECTS = "/api/software_factory/projects"


def _published(monkeypatch):
    events = []

    async def emit(name, **kwargs):
        events.append((name, kwargs["payload"]))

    monkeypatch.setattr("druks.contrib.software_factory.models.publish", emit)
    return events


def _ready(identifier, title, repo):
    return (
        "ticket.transitioned",
        {
            "source": "issues",
            "identifier": identifier,
            "status": Status.READY_FOR_AGENT.label,
            "title": title,
            "url": f"/software_factory/tickets/{identifier}",
            "project_name": repo,
            "labels": [],
            "assignee_email": None,
            "assignee_name": None,
        },
    )


async def _open_project(druks_client, name="Acme"):
    created = await druks_client.post(_PROJECTS, json={"name": name})
    assert created.status_code == 201
    return created.json()


async def _add_repo(druks_client, project, repo):
    added = await druks_client.post(f"{_PROJECTS}/{project['id']}/repos", json={"fullName": repo})
    assert added.status_code == 201
    return added.json()


async def _open_repo(druks_client, *, project="Acme", repo="acme/druks"):
    return await _add_repo(druks_client, await _open_project(druks_client, project), repo)


async def _open_ticket(druks_client, repo_id, **fields):
    created = await druks_client.post(
        _TICKETS,
        json={"title": "one", "repo_id": int(repo_id), **fields},
    )
    assert created.status_code == 201
    return created.json()


async def test_create_as_backlog_does_not_publish(druks_client, monkeypatch):
    events = _published(monkeypatch)
    repo = await _open_repo(druks_client)
    ticket = await _open_ticket(druks_client, repo["id"], title="quiet")

    assert (ticket["identifier"], ticket["status"], ticket["comments"]) == ("ACM-1", "backlog", [])
    assert events == []


async def test_create_as_ready_for_agent_publishes_the_trigger(druks_client, monkeypatch):
    events = _published(monkeypatch)
    repo = await _open_repo(druks_client, repo="acme/acme-app")
    await _open_ticket(druks_client, repo["id"], status="ready_for_agent", title="go")

    assert events == [_ready("ACM-1", "go", "acme/acme-app")]


async def test_set_status_publishes_one_transition(druks_client, monkeypatch):
    events = _published(monkeypatch)
    repo = await _open_repo(druks_client, repo="acme/acme-app")
    ticket = await _open_ticket(druks_client, repo["id"], title="Add an endpoint")

    for _ in range(2):
        moved = await druks_client.post(
            f"{_TICKETS}/{ticket['identifier']}/status",
            json={"status": "ready_for_agent"},
        )
        assert moved.status_code == 200
        assert moved.json()["status"] == "ready_for_agent"

    assert events == [_ready("ACM-1", "Add an endpoint", "acme/acme-app")]


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
    assert (body["title"], body["priority"], body["status"]) == ("new", "high", "backlog")
    assert events == []


async def test_blank_owner_is_nobody(druks_client):
    repo = await _open_repo(druks_client)
    created = await druks_client.post(
        _TICKETS,
        json={"title": "unheld", "repo_id": int(repo["id"]), "owner_id": ""},
    )
    assert created.status_code == 201
    assert created.json()["ownerId"] is None

    identifier = created.json()["identifier"]
    assigned = await Account.get_or_create("dev@example.com")
    held = await druks_client.patch(f"{_TICKETS}/{identifier}", json={"owner_id": assigned.id})
    assert held.json()["ownerId"] == assigned.id

    kept = await druks_client.patch(f"{_TICKETS}/{identifier}", json={"title": "still held"})
    assert kept.json()["ownerId"] == assigned.id

    cleared = await druks_client.patch(f"{_TICKETS}/{identifier}", json={"owner_id": ""})
    assert cleared.json()["ownerId"] is None


async def test_update_can_move_a_ticket_to_another_repo(druks_client):
    first = await _open_repo(druks_client, project="Alpha", repo="acme/alpha")
    second = await _open_repo(druks_client, project="Beta", repo="acme/beta")
    ticket = await _open_ticket(druks_client, first["id"])

    moved = await druks_client.patch(
        f"{_TICKETS}/{ticket['identifier']}",
        json={"repo_id": int(second["id"])},
    )

    assert moved.status_code == 200
    assert (moved.json()["repoId"], moved.json()["identifier"]) == (int(second["id"]), "ALP-1")


async def test_add_comment_authors_from_the_request_account(druks_client):
    account = await Account.get_or_create("op@example.com")
    repo = await _open_repo(druks_client)
    ticket = await _open_ticket(druks_client, repo["id"])

    written = await druks_client.post(
        f"{_TICKETS}/{ticket['identifier']}/comments",
        json={"body": "ship it"},
    )

    assert written.status_code == 201
    assert (written.json()["author"], written.json()["body"]) == (account.username, "ship it")
    detail = await druks_client.get(f"{_TICKETS}/{ticket['identifier']}")
    assert [line["author"] for line in detail.json()["comments"]] == [account.username]


async def test_blank_title_and_body_are_refused(druks_client):
    repo = await _open_repo(druks_client)

    created = await druks_client.post(
        _TICKETS,
        json={"title": "   ", "repo_id": int(repo["id"])},
    )
    assert created.status_code == 422

    ticket = await _open_ticket(druks_client, repo["id"])
    edited = await druks_client.patch(f"{_TICKETS}/{ticket['identifier']}", json={"title": " "})
    assert edited.status_code == 422

    commented = await druks_client.post(
        f"{_TICKETS}/{ticket['identifier']}/comments",
        json={"body": "\n"},
    )
    assert commented.status_code == 422


async def test_unknown_ticket_repo_and_owner_are_404(druks_client):
    assert (await druks_client.get(f"{_TICKETS}/DRU-99")).status_code == 404
    assert (
        await druks_client.post(_TICKETS, json={"title": "t", "repo_id": 999999})
    ).status_code == 404

    repo = await _open_repo(druks_client)
    assigned = await druks_client.post(
        _TICKETS,
        json={"title": "held by no one", "repo_id": int(repo["id"]), "owner_id": "not-an-account"},
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


async def test_deleting_a_repo_takes_its_tickets_and_comments(druks_client):
    project = await _open_project(druks_client)
    repo = await _add_repo(druks_client, project, "acme/gone")
    ticket = await _open_ticket(druks_client, repo["id"])
    await druks_client.post(f"{_TICKETS}/{ticket['identifier']}/comments", json={"body": "note"})

    deleted = await druks_client.delete(f"{_PROJECTS}/{project['id']}/repos/{repo['id']}")

    assert deleted.status_code == 204
    assert (await druks_client.get(f"{_TICKETS}/{ticket['identifier']}")).status_code == 404
