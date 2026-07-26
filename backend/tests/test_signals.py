import pytest
from conftest import make_test_work_item
from druks.contrib.ship.models import ProjectRepo, WorkItem
from druks.contrib.ship.workflows import Build, Profile
from druks.extensions.exceptions import SubscriberDeclarationError
from druks.signals import publish, subscribe
from druks.workflows import Workflow
from sqlalchemy.orm import object_session


def _work_item(**fields):
    return make_test_work_item(repo="ClawHaven/acme-app", title="probe", **fields)


@pytest.mark.asyncio
async def test_subscriber_failure_propagates_to_the_publisher():
    # The webhook dedup release and the DBOS lifecycle-step retry both rely on
    # publish failing loudly; a swallowed subscriber error would silently lose
    # the event (the provider's redelivery would short-circuit as a duplicate).
    @subscribe("test.subscriber_failure")
    async def boom(**_: object) -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await publish("test.subscriber_failure")


@pytest.mark.asyncio
async def test_subject_subscriber_receives_the_row(db_session):
    item = _work_item(remote_key="ENG-748-SIGNAL")
    received = []

    @subscribe("test.subject_row", subject=WorkItem)
    async def receive(*, subject: WorkItem) -> None:
        received.append(subject)

    await publish("test.subject_row", subject=item.identity)

    assert received == [item]


@pytest.mark.asyncio
async def test_deleted_subject_skips_the_subscriber(db_session):
    item = _work_item(remote_key="ENG-748-DELETED")
    identity = item.identity
    received = []

    @subscribe("test.deleted_subject", subject=WorkItem)
    async def receive(*, subject: WorkItem) -> None:
        received.append(subject)

    object_session(item).delete(item)
    object_session(item).flush()
    await publish("test.deleted_subject", subject=identity)

    assert received == []


@pytest.mark.asyncio
async def test_another_subjects_event_skips_the_subscriber(db_session):
    item = _work_item()
    repo = ProjectRepo.create(project_id=item.project_id, full_name="acme/other")
    received = []

    @subscribe("test.other_subject", subject=WorkItem)
    async def receive(*, subject: WorkItem) -> None:
        received.append(subject)

    await publish("test.other_subject", subject=repo.identity)
    await publish("test.other_subject", subject=None)

    assert received == []


@pytest.mark.asyncio
async def test_workflow_filter_narrows_to_that_workflow():
    # The body names the fact it came for; the kind it was matched on is routing,
    # so it never reaches the signature.
    received = []

    @subscribe("test.workflow_filter", workflow=Build)
    async def receive(*, ticket: str, **_: object) -> None:
        received.append(ticket)

    await publish("test.workflow_filter", kind=Profile.kind, ticket="ENG-1")
    await publish("test.workflow_filter", kind=Build.kind, ticket="ENG-2")

    assert received == ["ENG-2"]


def test_a_subscriber_asking_for_routing_fails_at_declaration():
    with pytest.raises(SubscriberDeclarationError, match=r"\['kind', 'run'\]"):

        @subscribe("test.routing_in_signature")
        async def receive(*, run: str, kind: str, **_: object) -> None: ...


def test_workflow_subject_requires_a_registered_class():
    workflow = Workflow()
    workflow._subject = {"type": "missing", "id": 1}

    with pytest.raises(LookupError, match="no subject class is named"):
        _ = workflow.subject
