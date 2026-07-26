import pytest
from druks.extensions.exceptions import SubscriberDeclarationError
from druks.signals import publish, subscribe
from druks.workflows import Workflow
from druks_field_notes.models import Note
from druks_field_notes.workflows import Summarize
from sqlalchemy.orm import object_session


def _note(body: str = "probe") -> Note:
    return Note.create(body=body)


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
async def test_subject_subscriber_receives_the_row(druks_db):
    item = _note()
    received = []

    @subscribe("test.subject_row", subject=Note)
    async def receive(*, subject: Note) -> None:
        received.append(subject)

    await publish("test.subject_row", subject=item.identity)

    assert received == [item]


@pytest.mark.asyncio
async def test_deleted_subject_skips_the_subscriber(druks_db):
    item = _note()
    identity = item.identity
    received = []

    @subscribe("test.deleted_subject", subject=Note)
    async def receive(*, subject: Note) -> None:
        received.append(subject)

    object_session(item).delete(item)
    object_session(item).flush()
    await publish("test.deleted_subject", subject=identity)

    assert received == []


@pytest.mark.asyncio
async def test_another_subjects_event_skips_the_subscriber(druks_db):
    item = _note()
    received = []

    @subscribe("test.other_subject", subject=Note)
    async def receive(*, subject: Note) -> None:
        received.append(subject)

    await publish("test.other_subject", subject={"type": "repo", "id": item.id})
    await publish("test.other_subject", subject=None)

    assert received == []


@pytest.mark.asyncio
async def test_workflow_filter_narrows_to_that_workflow():
    # The body names the fact it came for; the kind it was matched on is routing,
    # so it never reaches the signature.
    received = []

    @subscribe("test.workflow_filter", workflow=Summarize)
    async def receive(*, ticket: str, **_: object) -> None:
        received.append(ticket)

    await publish("test.workflow_filter", kind="field_notes.other", ticket="ENG-1")
    await publish("test.workflow_filter", kind=Summarize.kind, ticket="ENG-2")

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
