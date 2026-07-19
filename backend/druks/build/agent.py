# Build's half of the agent surface: the work board, one item's detail, and
# dispatch — shared by the /api/build/agent routes and the MCP tools.
import base64
from datetime import datetime

from sqlalchemy import select, tuple_

from druks.accounts import sessions
from druks.accounts.models import Account
from druks.build.exceptions import InvalidCursor, WorkItemNotFound
from druks.build.models import WorkItem
from druks.build.schemas import (
    AgentDispatchResult,
    AgentWorkItem,
    AgentWorkItemDetail,
    AgentWorkPage,
)
from druks.build.workflows import BuildWorkflow
from druks.database import db_session
from druks.durable.enums import ACTIVE_STATES, RunState
from druks.durable.models import Run
from druks.durable.reads import get_subject_status, list_recent_runs

# Sized so a page of maximal rows serializes under the 12KiB budget.
_PAGE_LIMIT = 12
_RECENT_RUNS_LIMIT = 5
_RUN_CALLS_LIMIT = 5

# The lifecycle filters, as predicates on the item's build run — the dedup
# anchor and resume handle. "mine" is the caller's runs; the rest read the
# run's derived state.
_STATE_FILTERS = {
    "parked": (RunState.PENDING_INPUT.value,),
    "active": tuple(state.value for state in ACTIVE_STATES),
    "failed": (RunState.FAILED.value,),
}


def list_work(
    account: Account, *, filter: str | None = None, cursor: str | None = None
) -> AgentWorkPage:
    stmt = (
        select(WorkItem)
        .order_by(WorkItem.updated_at.desc(), WorkItem.id.desc())
        .limit(_PAGE_LIMIT + 1)
    )
    if filter:
        stmt = stmt.join(Run, Run.id == WorkItem.build_run_id)
        if filter == "mine":
            stmt = stmt.where(Run.account_id == account.id)
        else:
            stmt = stmt.where(Run.state.in_(_STATE_FILTERS[filter]))
    if cursor:
        after_at, after_id = _decode_cursor(cursor)
        stmt = stmt.where(tuple_(WorkItem.updated_at, WorkItem.id) < (after_at, after_id))
    items = list(db_session().scalars(stmt))
    next_cursor = None
    if len(items) > _PAGE_LIMIT:
        items = items[:_PAGE_LIMIT]
        next_cursor = _encode_cursor(items[-1])
    return AgentWorkPage(
        items=[
            AgentWorkItem.from_work_item(item, get_subject_status("work_item", str(item.id)))
            for item in items
        ],
        next_cursor=next_cursor,
    )


def get_work_item(work_item_id: int) -> AgentWorkItemDetail:
    item = WorkItem.get(work_item_id)
    if not item:
        raise WorkItemNotFound(str(work_item_id))
    subject_id = str(item.id)
    return AgentWorkItemDetail.from_work_item(
        item,
        get_subject_status("work_item", subject_id),
        list_recent_runs(
            "work_item", subject_id, limit=_RECENT_RUNS_LIMIT, calls_limit=_RUN_CALLS_LIMIT
        ),
    )


async def dispatch(
    account: Account,
    *,
    work_item_id: int | None = None,
    source: str | None = None,
    ticket_ref: str | None = None,
) -> AgentDispatchResult:
    # The route holds the addressing to exactly one mode; both resolve an
    # existing item — the closed error taxonomy has no room for intake here.
    if work_item_id:
        item = WorkItem.get(work_item_id)
        ref = str(work_item_id)
    else:
        item = WorkItem.get_for_remote_key(source=source or "", remote_key=ticket_ref or "")
        ref = f"{source}:{ticket_ref}"
    if not item:
        raise WorkItemNotFound(ref)
    # Not every caller arrives through the session gate (the MCP boundary has
    # none), so the service stamps ambient attribution itself — start() inherits
    # it when the item carries no assignee.
    sessions.current_account_id.set(account.id)
    run_id = await BuildWorkflow.dispatch(work_item_id=item.id)
    run = Run.get(run_id)
    assert run is not None  # dispatch just created (or handed back) this run's row
    return AgentDispatchResult(
        work_item_id=item.id,
        run_id=run_id,
        is_owned_by_caller=run.account_id == account.id,
        note="run_id is the active build for this work item; dispatching again returns it.",
    )


def _encode_cursor(item: WorkItem) -> str:
    key = f"{item.updated_at.isoformat()}|{item.id}"
    return base64.urlsafe_b64encode(key.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, int]:
    try:
        updated_at, _, item_id = base64.urlsafe_b64decode(cursor.encode()).decode().partition("|")
        return datetime.fromisoformat(updated_at), int(item_id)
    except ValueError as error:
        # Covers base64, UTF-8, timestamp, and int parse failures alike.
        raise InvalidCursor() from error
