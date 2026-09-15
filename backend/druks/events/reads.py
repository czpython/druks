from sqlalchemy import Select, Text, func, select, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from druks.apps.loader import get_app
from druks.durable.models import Artifact, Run
from druks.events.feed import FeedDestinations
from druks.events.models import Event


async def list_events(
    session: AsyncSession, statement: Select[tuple[Event]]
) -> tuple[list[Event], str]:
    """Read events and their visibility snapshot in one statement, including an empty page."""
    snapshot = select(func.pg_current_snapshot().cast(Text).label("cursor")).cte("snapshot")
    event = aliased(Event, statement.subquery())
    rows = (
        await session.execute(
            select(snapshot.c.cursor, event)
            .select_from(snapshot)
            .outerjoin(event, true())
            .order_by(event.id.desc())
        )
    ).all()
    return [event for _, event in rows if event], rows[0].cursor


async def list_topics(session: AsyncSession, app: str | None) -> list[dict[str, str]]:
    statement = (
        Event.get_history(app=app)
        .with_only_columns(Event.app, Event.type)
        .distinct()
        .order_by(Event.app, Event.type)
    )
    return [{"app": owner, "topic": topic} for owner, topic in await session.execute(statement)]


async def get_destinations(session: AsyncSession, event: Event) -> FeedDestinations:
    """Whether the subject, run, and artifact that one Activity row recorded still exist."""
    subject_classes = {subject.subject_type: subject for subject in get_app(event.app).subjects()}
    subject_class = subject_classes.get(event.subject_type)
    subject = None
    if subject_class and event.subject_id:
        subject = await subject_class.get_for_subject_id(event.subject_id)
    run_id = await session.scalar(select(Run.id).where(Run.id == event.payload.get("run")))
    artifact_id = await session.scalar(
        select(Artifact.id).where(Artifact.id == event.payload.get("artifact_id"))
    )
    return FeedDestinations(
        is_subject_available=bool(subject),
        is_run_available=bool(run_id),
        is_artifact_available=bool(artifact_id),
    )
