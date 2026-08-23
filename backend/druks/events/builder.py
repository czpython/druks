from sqlalchemy import or_, select

from druks.database import db_session
from druks.events.feed import FeedItem
from druks.events.models import Event

_PAGE_LIMIT_DEFAULT = 200
_FETCH_LIMIT = 500


def build_feed(
    *,
    app: str | None = None,
    before: int | None = None,
    limit: int = _PAGE_LIMIT_DEFAULT,
) -> tuple[list[FeedItem], str | None]:
    items = [FeedItem.model_validate(event) for event in _events(app, before)]
    items.sort(key=lambda item: item.seq, reverse=True)
    page = items[:limit]
    next_cursor = str(page[-1].seq) if len(page) == limit and page else None
    return page, next_cursor


def _events(app: str | None, before: int | None) -> list[Event]:
    # This app's events plus any unscoped (core) ones. The log stores the app;
    # the core never derives it from the subject.
    stmt = select(Event).order_by(Event.id.desc())
    if before:
        stmt = stmt.where(Event.id < before)
    if app:
        stmt = stmt.where(or_(Event.app == app, Event.app.is_(None)))
    return list(db_session().scalars(stmt.limit(_FETCH_LIMIT)).all())
