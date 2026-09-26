import pytest
from druks.contrib.software_factory.models import Project, WorkItem
from druks.db import db_session
from druks.workflows import WorkflowError
from druks_field_notes.models import Repository


async def test_recorded_titles_and_facts_survive_rename_and_deletion(druks_db, druks_client):
    db_session.registry.set(druks_db)
    project = await Project.create(name="Pumps")
    item = await WorkItem.create(
        project=project, repo="acme/pumps", title="Pump 50%_ hot", ticket_key="PUMP-1"
    )
    facts = {"revision_number": 2, "inspection": {"sensor_id": "A", "readings": [0, 50]}}
    await item.announce("item.inspected", **facts)
    await item.update(title="Renamed work")
    await druks_db.delete(item)
    await druks_db.flush()
    other = await WorkItem.create(
        project=project, repo="acme/pumps", title="Pump 50ZZ hot", ticket_key="PUMP-2"
    )
    await other.announce("item.inspected", **facts)

    filters = {"app": "software_factory", "topic": "item.inspected", "q": "  pUMP 50%_  "}
    page = (await druks_client.get("/api/events", params={**filters, "limit": 1})).json()
    [recorded] = page["items"]
    assert page["nextCursor"] is None
    assert recorded["subjectKey"] == "PUMP-1"
    assert recorded["payload"] == {**facts, "title": "Pump 50%_ hot"}
    assert set(recorded) == {
        "id",
        "seq",
        "at",
        "topic",
        "app",
        "subjectType",
        "subjectId",
        "subjectKey",
        "payload",
    }
    assert not (await druks_client.get("/api/events", params={"q": "renamed"})).json()["items"]
    assert (await druks_client.get("/api/events", params={"q": "pump-1"})).json()["items"] == [
        recorded
    ]


async def test_a_summary_without_a_title_keeps_the_work_key_and_facts(druks_db, druks_client):
    db_session.registry.set(druks_db)
    repository = await Repository.create(repo="acme/observations")
    await repository.announce("repository.inspected", branch_name="main")

    [recorded] = (await druks_client.get("/api/events")).json()["items"]
    assert recorded["subjectKey"] == "acme/observations"
    assert recorded["payload"] == {"branch_name": "main"}


async def test_an_announcement_cannot_supply_what_druks_records(druks_db):
    db_session.registry.set(druks_db)
    repository = await Repository.create(repo="acme/observations")
    with pytest.raises(WorkflowError, match="title"):
        await repository.announce("repository.inspected", title="Forged title")
