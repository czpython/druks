from sqlalchemy import select

from druks.apps.loader import get_app
from druks.database import db_session
from druks.durable.models import Artifact, Run
from druks.events.feed import FeedDestinations
from druks.events.models import Event


async def list_kinds(app: str | None) -> list[str]:
    statement = (
        Event.get_history(app=app).with_only_columns(Event.type).distinct().order_by(Event.type)
    )
    return list(await db_session().scalars(statement))


async def get_destinations(event: Event) -> FeedDestinations:
    """Whether the subject, run, and artifact that one Activity row recorded still exist."""
    subject_classes = {subject.subject_type: subject for subject in get_app(event.app).subjects()}
    subject_class = subject_classes.get(event.subject_type)
    subject = None
    if subject_class and event.subject_id:
        subject = await subject_class.get_for_subject_id(event.subject_id)
    run_id = await db_session().scalar(select(Run.id).where(Run.id == event.payload.get("run")))
    artifact_id = await db_session().scalar(
        select(Artifact.id).where(Artifact.id == event.payload.get("artifact_id"))
    )
    return FeedDestinations(
        is_subject_available=bool(subject),
        is_run_available=bool(run_id),
        is_artifact_available=bool(artifact_id),
    )
