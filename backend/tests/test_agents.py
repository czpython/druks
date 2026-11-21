from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

from conftest import IDENTITY_HEADER, header_client, seed_call, seed_note_run
from druks.accounts.enums import AccountKind
from druks.accounts.models import Account
from druks.chat.models import Conversation
from druks.durable.dbos_state import workflow_status
from druks.durable.models import Schedule
from druks.skills.datastructures import InstalledSkill
from druks.skills.models import Skill, SkillCollection
from druks.teams.models import Team

AGENT = {
    "name": "Security engineer",
    "description": "Reviews changes for security problems.",
    "instructions": "Read the diff. Name each risk.",
    "skills": [],
    "harness": "claude",
    "model": "anthropic/claude-opus-4-7",
    "isPrivate": False,
}


async def test_a_member_creates_an_agent_and_owns_it(tmp_path: Path, druks_db):
    await Account.get_or_create(druks_db, "admin@example.com")
    member = await Account.get_or_create(druks_db, "member@example.com")
    headers = {IDENTITY_HEADER: member.username}

    with header_client(tmp_path) as client:
        created = client.post("/api/agents", json=AGENT, headers=headers)
        listed = client.get("/api/agents", headers=headers)

    assert created.status_code == 201
    assert created.json()["ownerId"] == member.id
    assert listed.json() == [created.json()]


async def test_the_owner_gives_an_agent_a_picture(tmp_path: Path, druks_db):
    await Account.get_or_create(druks_db, "admin@example.com")
    member = await Account.get_or_create(druks_db, "member@example.com")
    headers = {IDENTITY_HEADER: member.username}
    picture = "https://example.com/security.png"

    with header_client(tmp_path) as client:
        agent = client.post("/api/agents", json=AGENT, headers=headers).json()
        url = f"/api/agents/{agent['id']}"
        client.put(
            url,
            json={**AGENT, "ownerId": member.id, "channelId": None, "pictureUrl": picture},
            headers=headers,
        )
        fetched = client.get(url, headers=headers)

    assert fetched.json()["pictureUrl"] == picture


async def test_agent_names_are_unique_in_any_case(tmp_path: Path, druks_db):
    admin = await Account.get_or_create(druks_db, "admin@example.com")
    headers = {IDENTITY_HEADER: admin.username}

    with header_client(tmp_path) as client:
        first = client.post("/api/agents", json=AGENT, headers=headers)
        other = client.post("/api/agents", json={**AGENT, "name": "İnspector"}, headers=headers)
        dotted_duplicate = client.post(
            "/api/agents", json={**AGENT, "name": "İnspector"}, headers=headers
        )
        duplicate = client.post(
            "/api/agents",
            json={**AGENT, "name": "security engineer"},
            headers=headers,
        )
        renamed = client.put(
            f"/api/agents/{other.json()['id']}",
            json={
                **AGENT,
                "name": "SECURITY ENGINEER",
                "ownerId": admin.id,
                "channelId": None,
                "pictureUrl": None,
            },
            headers=headers,
        )

    assert first.status_code == 201
    assert dotted_duplicate.status_code == 422
    assert duplicate.status_code == 422
    assert renamed.status_code == 422


async def test_another_member_cannot_see_or_change_a_private_agent(tmp_path: Path, druks_db):
    admin = await Account.get_or_create(druks_db, "admin@example.com")
    owner = await Account.get_or_create(druks_db, "owner@example.com")
    other = await Account.get_or_create(druks_db, "other@example.com")
    owner_headers = {IDENTITY_HEADER: owner.username}
    other_headers = {IDENTITY_HEADER: other.username}

    with header_client(tmp_path) as client:
        private = client.post(
            "/api/agents", json={**AGENT, "isPrivate": True}, headers=owner_headers
        ).json()
        public = client.post(
            "/api/agents", json={**AGENT, "name": "Reviewer"}, headers=owner_headers
        ).json()
        listed_by_other = client.get("/api/agents", headers=other_headers)
        listed_by_admin = client.get("/api/agents", headers={IDENTITY_HEADER: admin.username})
        update = {**AGENT, "ownerId": owner.id, "channelId": None, "pictureUrl": None}
        private_url = f"/api/agents/{private['id']}"
        public_url = f"/api/agents/{public['id']}"
        private_requests = [
            client.get(private_url, headers=other_headers),
            client.put(private_url, json=update, headers=other_headers),
            client.delete(private_url, headers=other_headers),
            client.get(f"{private_url}/runs", headers=other_headers),
            client.get(f"{private_url}/schedules", headers=other_headers),
            client.get(
                f"{private_url}/cost",
                params={"since": "2026-11-16T00:00:00Z"},
                headers=other_headers,
            ),
        ]
        public_requests = [
            client.put(public_url, json={**update, "name": "Reviewer"}, headers=other_headers),
            client.delete(public_url, headers=other_headers),
        ]
        deleted_by_owner = client.delete(private_url, headers=owner_headers)

    assert [agent["id"] for agent in listed_by_other.json()] == [public["id"]]
    assert {agent["id"] for agent in listed_by_admin.json()} == {private["id"], public["id"]}
    assert [response.status_code for response in private_requests] == [404] * 6
    assert [response.status_code for response in public_requests] == [403, 403]
    assert deleted_by_owner.status_code == 204


async def test_only_an_admin_changes_the_owner(tmp_path: Path, druks_db):
    admin = await Account.get_or_create(druks_db, "admin@example.com")
    owner = await Account.get_or_create(druks_db, "owner@example.com")
    other = await Account.get_or_create(druks_db, "other@example.com")
    bot = await Account.create_for_bot(druks_db, AccountKind.BOT)

    with header_client(tmp_path) as client:
        agent = client.post(
            "/api/agents", json=AGENT, headers={IDENTITY_HEADER: owner.username}
        ).json()
        url = f"/api/agents/{agent['id']}"
        by_owner = client.put(
            url,
            json={**AGENT, "ownerId": other.id, "channelId": None, "pictureUrl": None},
            headers={IDENTITY_HEADER: owner.username},
        )
        by_admin = client.put(
            url,
            json={**AGENT, "ownerId": other.id, "channelId": None, "pictureUrl": None},
            headers={IDENTITY_HEADER: admin.username},
        )
        not_operators = [
            client.put(
                url,
                json={**AGENT, "ownerId": owner_id, "channelId": None, "pictureUrl": None},
                headers={IDENTITY_HEADER: admin.username},
            )
            for owner_id in (bot.id, "no-such-id")
        ]

    assert by_owner.status_code == 403
    assert by_admin.status_code == 200
    assert by_admin.json()["ownerId"] == other.id
    assert [response.status_code for response in not_operators] == [422, 422]


async def test_only_an_admin_gives_an_agent_a_channel_the_bot_is_in(
    tmp_path: Path, druks_db, monkeypatch
):
    channels = AsyncMock(return_value=[{"id": "C1", "name": "security"}])
    monkeypatch.setattr("druks.agents.routes.list_notification_channels", channels)
    admin = await Account.get_or_create(druks_db, "admin@example.com")
    owner = await Account.get_or_create(druks_db, "owner@example.com")

    with header_client(tmp_path) as client:
        agent = client.post(
            "/api/agents", json=AGENT, headers={IDENTITY_HEADER: owner.username}
        ).json()
        url = f"/api/agents/{agent['id']}"
        update = {**AGENT, "ownerId": owner.id, "channelId": "C1", "pictureUrl": None}
        by_owner = client.put(url, json=update, headers={IDENTITY_HEADER: owner.username})
        outside = client.put(
            url, json={**update, "channelId": "C2"}, headers={IDENTITY_HEADER: admin.username}
        )
        by_admin = client.put(url, json=update, headers={IDENTITY_HEADER: admin.username})
        # The bot has left the channel since: an edit that keeps the channel still saves.
        channels.return_value = []
        kept_by_owner = client.put(
            url, json={**update, "name": "Security"}, headers={IDENTITY_HEADER: owner.username}
        )

    assert by_owner.status_code == 403
    assert outside.status_code == 422
    assert by_admin.json()["channelId"] == "C1"
    assert kept_by_owner.json()["channelId"] == "C1"


async def test_a_channel_has_one_agent_and_only_a_public_one(tmp_path: Path, druks_db, monkeypatch):
    monkeypatch.setattr(
        "druks.agents.routes.list_notification_channels",
        AsyncMock(return_value=[{"id": "C1", "name": "security"}]),
    )
    admin = await Account.get_or_create(druks_db, "admin@example.com")
    headers = {IDENTITY_HEADER: admin.username}

    with header_client(tmp_path) as client:
        first = client.post("/api/agents", json=AGENT, headers=headers).json()
        second = client.post(
            "/api/agents", json={**AGENT, "name": "Writer"}, headers=headers
        ).json()
        bound = {**AGENT, "ownerId": admin.id, "channelId": "C1", "pictureUrl": None}
        client.put(f"/api/agents/{first['id']}", json=bound, headers=headers)
        taken = client.put(
            f"/api/agents/{second['id']}",
            json={**bound, "name": "Writer"},
            headers=headers,
        )
        private = client.put(
            f"/api/agents/{first['id']}",
            json={**bound, "isPrivate": True},
            headers=headers,
        )

    assert taken.status_code == 422
    assert "Security engineer" in taken.json()["detail"]
    assert private.status_code == 422


async def test_an_agent_takes_enabled_skills_and_a_harness_that_runs_it(tmp_path: Path, druks_db):
    admin = await Account.get_or_create(druks_db, "admin@example.com")
    headers = {IDENTITY_HEADER: admin.username}
    await SkillCollection.create(
        druks_db,
        source="https://github.com/o/r",
        name="o/r",
        skills=[
            InstalledSkill(name="alpha", description="one", path="/p/alpha", content_hash="a"),
            InstalledSkill(name="beta", description="two", path="/p/beta", content_hash="b"),
        ],
    )
    (await Skill.get(druks_db, "beta")).enabled = False
    await druks_db.flush()

    with header_client(tmp_path) as client:
        enabled_skill = client.post(
            "/api/agents", json={**AGENT, "skills": ["alpha"]}, headers=headers
        )
        disabled_skill = client.post(
            "/api/agents",
            json={**AGENT, "name": "Reviewer", "skills": ["beta"]},
            headers=headers,
        )
        public_on_api_key_harness = client.post(
            "/api/agents",
            json={**AGENT, "name": "Reviewer", "harness": "opencode"},
            headers=headers,
        )
        public_on_harness_without_chat = client.post(
            "/api/agents",
            json={**AGENT, "name": "Pi", "harness": "pi"},
            headers=headers,
        )
        private_on_api_key_harness = client.post(
            "/api/agents",
            json={**AGENT, "name": "Tester", "harness": "opencode", "isPrivate": True},
            headers=headers,
        )
        wrong_model = client.post(
            "/api/agents",
            json={**AGENT, "name": "Planner", "harness": "claude", "model": "openai/gpt-5.5"},
            headers=headers,
        )

    assert enabled_skill.status_code == 201
    assert enabled_skill.json()["skills"] == ["alpha"]
    assert disabled_skill.status_code == 422
    assert public_on_api_key_harness.status_code == 201
    assert public_on_harness_without_chat.status_code == 422
    assert private_on_api_key_harness.status_code == 422
    assert wrong_model.status_code == 422


async def test_an_agent_lists_its_runs_that_a_person_can_open(tmp_path: Path, druks_db):
    await Account.get_or_create(druks_db, "admin@example.com")
    owner = await Account.get_or_create(druks_db, "owner@example.com")
    outsider = await Account.get_or_create(druks_db, "outsider@example.com")
    await Team.create(druks_db, name="Notes", member_ids=[owner.id], apps=["field_notes"])
    await druks_db.flush()

    with header_client(tmp_path) as client:
        agent = client.post(
            "/api/agents", json=AGENT, headers={IDENTITY_HEADER: owner.username}
        ).json()
        asked = await Conversation.create(
            druks_db, account_id=owner.id, body="Summarize", agent_id=agent["id"]
        )
        plain = await Conversation.create(druks_db, account_id=owner.id, body="Summarize")
        runs = []
        for conversation, cost in ((asked, 1.0), (asked, 2.0), (plain, 4.0)):
            run = await seed_note_run(druks_db, account_id=owner.id)
            run.conversation_id = conversation.id
            (await seed_call(druks_db, run, "summarize")).cost_usd = cost
            runs.append(run)
        runs[0].created_at = datetime.now(UTC) - timedelta(days=8)
        await druks_db.flush()
        url = f"/api/agents/{agent['id']}/runs"
        week = {"since": (datetime.now(UTC) - timedelta(days=7)).isoformat()}
        seen = client.get(url, headers={IDENTITY_HEADER: owner.username})
        hidden = client.get(url, headers={IDENTITY_HEADER: outsider.username})
        cost_url = f"/api/agents/{agent['id']}/cost"
        local = client.get(
            cost_url,
            params={"since": "2026-11-16T00:00:00"},
            headers={IDENTITY_HEADER: owner.username},
        )
        ever = {"since": "2000-01-01T00:00:00Z"}
        cost = client.get(cost_url, params=ever, headers={IDENTITY_HEADER: owner.username})
        week_cost = client.get(cost_url, params=week, headers={IDENTITY_HEADER: owner.username})
        hidden_cost = client.get(
            cost_url, params=ever, headers={IDENTITY_HEADER: outsider.username}
        )

    assert local.status_code == 422
    assert [(run["id"], run["accountUsername"]) for run in seen.json()] == [
        (runs[1].id, owner.username),
        (runs[0].id, owner.username),
    ]
    assert hidden.json() == []
    assert cost.json() == {"costUsd": 3.0}
    assert week_cost.json() == {"costUsd": 2.0}
    assert hidden_cost.json() == {"costUsd": 0.0}


async def test_an_agent_lists_the_schedules_set_in_its_conversations(tmp_path: Path, druks_db):
    await Account.get_or_create(druks_db, "admin@example.com")
    owner = await Account.get_or_create(druks_db, "owner@example.com")
    member = await Account.get_or_create(druks_db, "member@example.com")
    outsider = await Account.get_or_create(druks_db, "outsider@example.com")
    await Team.create(
        druks_db, name="Notes", member_ids=[owner.id, member.id], apps=["field_notes"]
    )
    await druks_db.flush()

    with header_client(tmp_path) as client:
        agent = client.post(
            "/api/agents", json=AGENT, headers={IDENTITY_HEADER: owner.username}
        ).json()
        schedules = []
        for agent_id in (agent["id"], None):
            conversation = await Conversation.create(
                druks_db, account_id=owner.id, body="Scan payments", agent_id=agent_id
            )
            schedule = await Schedule.create(
                druks_db,
                account_id=owner.id,
                conversation_id=conversation.id,
                tool="field_notes_jot_note",
                arguments={"body": "Scan payments"},
                cron="0 9 * * *",
                tool_names=["field_notes_jot_note"],
            )
            schedules.append(schedule)
        due_time = int(datetime(2026, 11, 2, 9, tzinfo=UTC).timestamp() * 1000)
        for index, status in enumerate(["SUCCESS", "ERROR"]):
            await druks_db.execute(
                workflow_status.insert().values(
                    workflow_uuid=f"due-time-{index}",
                    schedule_name=schedules[0].schedule_name,
                    status=status,
                    created_at=due_time + index * 86_400_000,
                )
            )
        url = f"/api/agents/{agent['id']}/schedules"
        listed = client.get(url, headers={IDENTITY_HEADER: member.username})
        hidden = client.get(url, headers={IDENTITY_HEADER: outsider.username})

    assert hidden.json() == []
    [shown] = listed.json()
    assert (shown["id"], shown["accountId"]) == (schedules[0].id, owner.id)
    assert (shown["lastRun"]["run"], shown["lastRun"]["status"]) == ("due-time-1", "ERROR")
