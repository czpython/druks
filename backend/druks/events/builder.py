from datetime import datetime

from sqlalchemy import select

from druks.apps.loader import iter_apps
from druks.database import db_session
from druks.durable.models import AgentCall, Artifact, Run
from druks.events.feed import FeedItem
from druks.events.models import Event


async def build_feed(
    *,
    app: str | None = None,
    q: str | None = None,
    kind: str | None = None,
    from_at: datetime | None = None,
    until: datetime | None = None,
    before: int | None = None,
    after: int | None = None,
    limit: int = 200,
) -> tuple[list[FeedItem], str | None]:
    """Read one page of recorded Activity and its current destination availability."""
    statement = Event.get_history(app=app).order_by(Event.id.desc())
    if q and q.strip():
        pattern = q.strip().replace("/", "//").replace("%", "/%").replace("_", "/_")
        statement = statement.where(Event.subject_label.ilike(f"%{pattern}%", escape="/"))
    if kind is not None:
        statement = statement.where(Event.type == kind)
    if from_at:
        statement = statement.where(Event.created_at >= from_at)
    if until:
        statement = statement.where(Event.created_at < until)
    if before is not None:
        statement = statement.where(Event.id < before)
    if after is not None:
        statement = statement.where(Event.id > after)
    events = list(await db_session().scalars(statement.limit(limit + 1)))
    page = [FeedItem.model_validate(event) for event in events[:limit]]
    next_cursor = str(page[-1].seq) if len(events) > limit else None
    run_ids = {item.run for item in page if item.run}
    runs = (
        set(await db_session().scalars(select(Run.id).where(Run.id.in_(run_ids))))
        if run_ids
        else set()
    )
    artifact_ids = {item.artifact_id for item in page if item.artifact_id}
    artifacts = {}
    if artifact_ids:
        rows = await db_session().execute(
            select(Artifact, AgentCall)
            .join(AgentCall, AgentCall.id == Artifact.agent_call_id)
            .where(Artifact.id.in_(artifact_ids))
        )
        artifacts = {
            artifact.id: bool(call.get_file_path(artifact.path)) for artifact, call in rows
        }
    subjects = {
        (owner.name, subject.subject_type): subject
        for owner in iter_apps()
        if not owner.builtin
        for subject in owner.subjects()
    }
    available_subjects = {}
    for item in page:
        identity = (item.app, item.subject_type, item.subject_id)
        if identity not in available_subjects:
            subject = subjects.get((item.app, item.subject_type))
            available_subjects[identity] = bool(
                subject and item.subject_id and await subject.get_for_subject_id(item.subject_id)
            )
        item.is_subject_available = available_subjects[identity]
        item.is_run_available = item.run in runs
        item.is_artifact_available = artifacts.get(item.artifact_id, False)
    return page, next_cursor
