import pytest
from druks.apps.exceptions import SubscriberDeclarationError
from druks.database import db_session
from druks.signals import publish, subscribe
from druks_field_notes.models import Note
from druks_field_notes.workflows import Summarize


async def _note(body: str = "probe") -> Note:
    return await Note.create(body=body)


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
    item = await _note()
    received = []

    @subscribe("test.subject_row", subject=Note)
    async def receive(*, subject: Note) -> None:
        received.append(subject)

    await publish("test.subject_row", subject=item.identity)

    assert received == [item]


@pytest.mark.asyncio
async def test_deleted_subject_skips_the_subscriber(druks_db):
    item = await _note()
    identity = item.identity
    received = []

    @subscribe("test.deleted_subject", subject=Note)
    async def receive(*, subject: Note) -> None:
        received.append(subject)

    await db_session().delete(item)
    await db_session().flush()
    await publish("test.deleted_subject", subject=identity)

    assert received == []


@pytest.mark.asyncio
async def test_another_subjects_event_skips_the_subscriber(druks_db):
    item = await _note()
    received = []

    @subscribe("test.other_subject", subject=Note)
    async def receive(*, subject: Note) -> None:
        received.append(subject)

    await publish("test.other_subject", subject={"type": "repo", "id": item.id})
    await publish("test.other_subject", subject=None)

    assert received == []


@pytest.mark.asyncio
async def test_workflow_filter_narrows_to_that_workflow(druks_db):
    # The body names the fact it came for; the kind it was matched on is routing,
    # so it never reaches the signature. Summarize declares its subject, so the
    # filter narrows to notes too and the body is handed the row.
    item = await _note()
    received = []

    @subscribe("test.workflow_filter", workflow=Summarize)
    async def receive(*, subject: Note, ticket: str, **_: object) -> None:
        received.append((subject, ticket))

    await publish(
        "test.workflow_filter", kind="field_notes.other", subject=item.identity, ticket="ENG-1"
    )
    await publish("test.workflow_filter", kind=Summarize.kind, subject=None, ticket="ENG-2")
    await publish(
        "test.workflow_filter", kind=Summarize.kind, subject=item.identity, ticket="ENG-3"
    )

    assert received == [(item, "ENG-3")]


def test_a_workflows_subject_and_an_explicit_one_are_never_written_together():
    with pytest.raises(SubscriberDeclarationError, match="already declares"):

        @subscribe("test.both_spellings", workflow=Summarize, subject=Note)
        async def receive(*, subject: Note, **_: object) -> None: ...


def test_a_subject_filter_must_name_a_subject_class():
    with pytest.raises(SubscriberDeclarationError, match="not a Subject or StoredSubject"):

        @subscribe("test.not_a_subject", subject=Summarize)
        async def receive(**_: object) -> None: ...


def test_a_subscriber_asking_for_routing_fails_at_declaration():
    with pytest.raises(SubscriberDeclarationError, match=r"\['kind', 'run'\]"):

        @subscribe("test.routing_in_signature")
        async def receive(*, run: str, kind: str, **_: object) -> None: ...


async def test_a_run_resolves_its_subject_through_the_declaration(druks_db):
    item = await _note()
    workflow = Summarize()
    workflow._subject = item.identity

    assert await workflow.subject is item
