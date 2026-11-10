from pathlib import Path

import pytest
from conftest import IDENTITY_HEADER, header_client
from druks.accounts.enums import AccountKind
from druks.accounts.models import Account
from druks.apps.loader import get_app
from druks.teams.models import Team


async def test_an_account_can_use_an_app_that_its_team_lists_or_that_no_team_lists(druks_db):
    admin = await Account.get_or_create(druks_db, "admin@example.com")
    first = await Account.get_or_create(druks_db, "first@example.com")
    second = await Account.get_or_create(druks_db, "second@example.com")
    outsider = await Account.get_or_create(druks_db, "outsider@example.com")
    bot = await Account.create_for_bot(druks_db, AccountKind.BOT)
    await Team.create(druks_db, name="First", member_ids=[first.id], apps=["field_notes"])
    await Team.create(druks_db, name="Second", member_ids=[second.id], apps=["field_notes"])
    druks_db.add(Team(name="Stored mistake", members=[], apps=["core"]))
    await druks_db.flush()
    field_notes = get_app("field_notes")

    assert await admin.can_use_app(field_notes)
    assert await first.can_use_app(field_notes)
    assert await second.can_use_app(field_notes)
    assert await bot.can_use_app(field_notes)
    assert not await outsider.can_use_app(field_notes)
    assert await outsider.can_use_app(get_app("software_factory"))
    assert await outsider.can_use_app(get_app("core"))


async def test_admin_creates_lists_and_replaces_a_team(tmp_path: Path, druks_db):
    admin = await Account.get_or_create(druks_db, "admin@example.com")
    first = await Account.get_or_create(druks_db, "first@example.com")
    second = await Account.get_or_create(druks_db, "second@example.com")
    headers = {IDENTITY_HEADER: admin.username}
    body = {
        "name": "Engineering",
        "memberIds": [first.id, second.id],
        "apps": ["field_notes"],
    }

    with header_client(tmp_path) as client:
        created = client.post("/api/teams", json=body, headers=headers)
        listed = client.get("/api/teams", headers=headers)

        assert created.status_code == 201
        assert created.json()["name"] == "Engineering"
        assert {member["username"] for member in created.json()["members"]} == {
            first.username,
            second.username,
        }
        assert created.json()["apps"] == ["field_notes"]
        assert listed.status_code == 200
        assert listed.json() == [created.json()]

        replaced = client.put(
            f"/api/teams/{created.json()['id']}",
            json={"name": "Platform", "memberIds": [first.id], "apps": ["field_notes"]},
            headers=headers,
        )

        assert replaced.status_code == 200
        assert replaced.json()["name"] == "Platform"
        assert [member["id"] for member in replaced.json()["members"]] == [first.id]


async def test_member_is_refused_on_each_team_route(tmp_path: Path, druks_db):
    await Account.get_or_create(druks_db, "admin@example.com")
    member = await Account.get_or_create(druks_db, "member@example.com")
    headers = {IDENTITY_HEADER: member.username}
    body = {"name": "Team", "memberIds": [], "apps": []}

    with header_client(tmp_path) as client:
        requests = [
            client.get("/api/teams", headers=headers),
            client.post("/api/teams", json=body, headers=headers),
            client.put("/api/teams/unknown", json=body, headers=headers),
            client.delete("/api/teams/unknown", headers=headers),
        ]

    assert [response.status_code for response in requests] == [403, 403, 403, 403]


async def test_team_names_are_trimmed_unique_and_not_empty(tmp_path: Path, druks_db):
    admin = await Account.get_or_create(druks_db, "admin@example.com")
    headers = {IDENTITY_HEADER: admin.username}

    with header_client(tmp_path) as client:
        first = client.post(
            "/api/teams",
            json={"name": " Engineering ", "memberIds": [], "apps": []},
            headers=headers,
        )
        duplicate = client.post(
            "/api/teams",
            json={"name": "Engineering", "memberIds": [], "apps": []},
            headers=headers,
        )
        empty = client.post(
            "/api/teams",
            json={"name": "  ", "memberIds": [], "apps": []},
            headers=headers,
        )
        missing = {"name": "Missing", "memberIds": [], "apps": []}
        updated_missing = client.put("/api/teams/unknown", json=missing, headers=headers)
        deleted_missing = client.delete("/api/teams/unknown", headers=headers)

    assert first.status_code == 201
    assert first.json()["name"] == "Engineering"
    assert duplicate.status_code == 422
    assert empty.status_code == 422
    assert updated_missing.status_code == 404
    assert deleted_missing.status_code == 404


async def test_built_in_and_unknown_apps_are_refused(tmp_path: Path, druks_db):
    admin = await Account.get_or_create(druks_db, "admin@example.com")
    headers = {IDENTITY_HEADER: admin.username}

    with header_client(tmp_path) as client:
        for app_name in ("core", "unknown"):
            response = client.post(
                "/api/teams",
                json={"name": app_name, "memberIds": [], "apps": [app_name]},
                headers=headers,
            )

            assert response.status_code == 422


@pytest.mark.parametrize("kind", [AccountKind.BOT, AccountKind.BOT_ADMIN])
async def test_bot_accounts_are_refused_as_members(kind: AccountKind, tmp_path: Path, druks_db):
    admin = await Account.get_or_create(druks_db, "admin@example.com")
    bot = await Account.create_for_bot(druks_db, kind)

    with header_client(tmp_path) as client:
        response = client.post(
            "/api/teams",
            json={"name": "Bots", "memberIds": [bot.id], "apps": []},
            headers={IDENTITY_HEADER: admin.username},
        )

    assert response.status_code == 422


async def test_deleting_a_team_keeps_its_accounts(tmp_path: Path, druks_db):
    admin = await Account.get_or_create(druks_db, "admin@example.com")
    member = await Account.get_or_create(druks_db, "member@example.com")
    headers = {IDENTITY_HEADER: admin.username}

    with header_client(tmp_path) as client:
        created = client.post(
            "/api/teams",
            json={
                "name": "Engineering",
                "memberIds": [admin.id, member.id],
                "apps": ["field_notes"],
            },
            headers=headers,
        )
        deleted = client.delete(f"/api/teams/{created.json()['id']}", headers=headers)
        listed = client.get("/api/teams", headers=headers)

    assert created.status_code == 201
    assert deleted.status_code == 204
    assert listed.json() == []
    assert {account.username for account in await Account.list_operators(druks_db)} == {
        admin.username,
        member.username,
    }
