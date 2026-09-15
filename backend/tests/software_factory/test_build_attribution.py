from types import SimpleNamespace

import pytest
from conftest import connect_service
from druks.accounts.models import Account
from druks.contrib.software_factory.app import SoftwareFactory
from druks.contrib.software_factory.enums import Status
from druks.contrib.software_factory.models import Project, ProjectRepo, Ticket
from druks.contrib.software_factory.ticketing.jira import Jira
from druks.contrib.software_factory.ticketing.linear import Linear
from druks.contrib.software_factory.webhooks import JiraEvents, LinearEvents
from druks.contrib.software_factory.workflows import Build
from druks.secrets.enums import IdentityStatus
from druks.secrets.models import VaultSecret
from druks.testing import make_settings

from software_factory.factories import make_test_work_item


@pytest.fixture
async def started(monkeypatch):
    await connect_service(
        "github",
        identity={"app_id": "1", "slug": "druks-operator"},
        secrets={"private_key": "operator-pem", "webhook_secret": "hook-secret"},
    )
    calls = []

    async def start(cls, **kwargs):
        calls.append(kwargs)
        return "new-build"

    monkeypatch.setattr(Build, "start", classmethod(start))
    return calls


@pytest.mark.parametrize(("source", "tracker"), [("jira", Jira), ("linear", Linear)])
async def test_tracker_webhook_starts_under_the_connected_account(
    druks_db, tmp_path, monkeypatch, started, source, tracker
):
    owner = await Account.get_or_create(druks_db, "github-boss")
    await VaultSecret.connect(
        druks_db,
        "mcp:renamed_company_tracker",
        account_id=owner.id,
        refresh_token="rt",
        scopes=["openid", "email"],
        identity={
            "authority": tracker.authority,
            "subject": "provider-user-1",
            "email": "boss@company.test",
            "source": "id_token",
        },
        identity_status=IdentityStatus.RESOLVED,
    )
    item = await make_test_work_item(repo="company/app", source=source, title="A change")
    settings = SoftwareFactory.Settings(tracker=source)

    async def get_settings(cls):
        return settings

    monkeypatch.setattr(SoftwareFactory, "settings", classmethod(get_settings))
    if source == "jira":
        await connect_service(
            "jira",
            identity={"base_url": "https://company.atlassian.net", "email": "druks@company.test"},
            secrets={"api_token": "token", "webhook_secret": "hook"},
        )
        events = JiraEvents(request=SimpleNamespace(), kwargs={}, settings=make_settings(tmp_path))
        events._data_cached = {
            "issue": {
                "key": item.ticket_key,
                "fields": {
                    "status": {"name": settings.trigger_status},
                    "summary": item.title,
                    "project": {"name": "company/app"},
                    "labels": [],
                    # Atlassian privacy settings can hide the email. The account ID stays.
                    "assignee": {"accountId": "provider-user-1", "displayName": "Boss"},
                },
            },
        }
        await events.on_issue_event()
    else:
        await connect_service(
            "linear",
            identity={"actor": "druks", "workspace": "Company"},
            secrets={"api_key": "key", "webhook_secret": "hook"},
        )
        events = LinearEvents(
            request=SimpleNamespace(), kwargs={}, settings=make_settings(tmp_path)
        )
        events._data_cached = {
            "data": {
                "identifier": item.ticket_key,
                "title": item.title,
                "url": f"https://linear.app/company/issue/{item.ticket_key}",
                "state": {"name": settings.trigger_status, "type": "unstarted"},
                "project": {"name": "company/app"},
                "assignee": {
                    "id": "provider-user-1",
                    "email": "boss@company.test",
                    "name": "Boss",
                },
            },
        }
        await events.on_state_transition()

    assert len(started) == 1
    assert started[0]["account_id"] == owner.id


async def test_unconnected_assignee_uses_the_default_account(druks_db, started):
    await Account.get_or_create(druks_db, "boss@company.test")
    await connect_service(
        "linear",
        identity={"actor": "druks", "workspace": "Company"},
        secrets={"api_key": "key", "webhook_secret": "hook"},
    )
    item = await make_test_work_item(repo="company/app", source="linear", title="A change")

    await Build.dispatch(
        ticket={
            "source": item.source,
            "identifier": item.ticket_key,
            "status": "Ready",
            "title": item.title,
            "url": f"https://tracker.example/{item.ticket_key}",
            "project_name": "company/app",
            "labels": [],
            "assignee_id": "provider-user-1",
            "assignee_email": "boss@company.test",
            "assignee_name": "Boss",
        }
    )

    assert not started[0]["account_id"]


async def test_builtin_ticket_uses_its_account_id(druks_db, monkeypatch, started):
    assignee = await Account.get_or_create(druks_db, "github-boss")
    project = await Project.create(name="Company")
    project_repo = await ProjectRepo.create(project_id=project.id, full_name="company/app")
    settings = SoftwareFactory.Settings(tracker="druks")

    async def get_settings(cls):
        return settings

    monkeypatch.setattr(SoftwareFactory, "settings", classmethod(get_settings))
    ticket = await Ticket.create(
        title="A change", project_repo=project_repo, assignee_id=assignee.id
    )

    await ticket.transition(Status.READY_FOR_AGENT)

    assert len(started) == 1
    assert started[0]["account_id"] == assignee.id
