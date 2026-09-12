from druks.accounts.models import Account
from druks.contrib.software_factory.enums import Status

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
            "source": "druks",
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


async def test_a_new_ticket_lands_in_backlog_and_tells_nobody(druks_client, monkeypatch):
    events = _published(monkeypatch)
    repo = await _open_repo(druks_client)

    ticket = await _open_ticket(druks_client, repo["id"], title="quiet")

    assert (ticket["identifier"], ticket["status"], ticket["comments"]) == ("ACM-1", "backlog", [])
    assert events == []


async def test_ready_for_agent_publishes_one_transition(druks_client, monkeypatch):
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


async def test_an_edit_takes_what_it_is_given_and_leaves_the_rest(druks_client, monkeypatch):
    events = _published(monkeypatch)
    here = await _open_repo(druks_client, project="Alpha", repo="acme/alpha")
    there = await _open_repo(druks_client, project="Beta", repo="acme/beta")
    owner = await Account.get_or_create("dev@example.com")
    ticket = await _open_ticket(druks_client, here["id"], title="old", owner_id=owner.id)

    async def edit(**fields):
        answer = await druks_client.patch(f"{_TICKETS}/{ticket['identifier']}", json=fields)
        assert answer.status_code == 200
        return answer.json()

    # Status is set_status's alone, and the identifier outlives a move.
    edited = await edit(title="new", priority="high", status="done")
    assert (edited["title"], edited["priority"], edited["status"]) == ("new", "high", "backlog")
    assert (await edit(repo_id=int(there["id"])))["identifier"] == "ALP-1"
    assert (await edit(title="kept"))["ownerId"] == owner.id
    assert (await edit(owner_id=""))["ownerId"] is None
    assert events == []


async def test_a_comment_carries_the_calling_account(druks_client):
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


async def test_blank_text_and_unknown_ids_are_refused(druks_client):
    repo = await _open_repo(druks_client)
    ticket = await _open_ticket(druks_client, repo["id"])

    async def status_of(method, path, **json):
        return (await getattr(druks_client, method)(path, json=json or None)).status_code

    assert await status_of("post", _TICKETS, title="   ", repo_id=int(repo["id"])) == 422
    assert await status_of("patch", f"{_TICKETS}/{ticket['identifier']}", title=" ") == 422
    assert await status_of("post", f"{_TICKETS}/{ticket['identifier']}/comments", body="\n") == 422
    assert await status_of("post", _TICKETS, title="t", repo_id=999999) == 404
    assert await status_of("patch", f"{_TICKETS}/{ticket['identifier']}", owner_id="nobody") == 404
    assert await status_of("post", f"{_TICKETS}/NOPE-1/status", status="done") == 404
    assert (await druks_client.get(f"{_TICKETS}/DRU-99")).status_code == 404


async def test_deleting_a_repo_takes_its_tickets_and_comments(druks_client):
    project = await _open_project(druks_client)
    repo = await _add_repo(druks_client, project, "acme/gone")
    ticket = await _open_ticket(druks_client, repo["id"])
    await druks_client.post(f"{_TICKETS}/{ticket['identifier']}/comments", json={"body": "note"})

    deleted = await druks_client.delete(f"{_PROJECTS}/{project['id']}/repos/{repo['id']}")

    assert deleted.status_code == 204
    assert (await druks_client.get(f"{_TICKETS}/{ticket['identifier']}")).status_code == 404
