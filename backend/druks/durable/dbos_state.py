from datetime import timedelta

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from druks.durable.enums import RunState

# DBOS keeps its bookkeeping in this schema of the app database (init_dbos
# points both DBOS urls at it), so a run's state is one correlated read away.
DBOS_SYSTEM_SCHEMA = "dbos"

# start() commits the durable_runs row before DBOS commits the enqueue, so a
# brand-new run legitimately has no workflow_status row for a moment. A run
# still rowless past this window will never start (DBOS system tables wiped, or
# its executor destroyed) and reads orphaned rather than scheduled forever.
_MISSING_STATUS_GRACE = timedelta(minutes=5)

# Read-only handle on the columns the derivation and subject keying need; DBOS
# owns and migrates the real table. NOTE: DBOS workflow_status GC/retention must
# stay off — a purged workflow row derives as orphaned once past the grace
# window and falls out of its subject's timeline.
workflow_status = sa.Table(
    "workflow_status",
    sa.MetaData(schema=DBOS_SYSTEM_SCHEMA),
    sa.Column("workflow_uuid", sa.String, primary_key=True),
    sa.Column("status", sa.String),
    sa.Column("updated_at", sa.BigInteger),
    sa.Column("attributes", JSONB),
)


def subject_filter(
    run_id: sa.ColumnElement, subject_type: str, subject_id: str
) -> sa.ColumnElement:
    # "This run is about that subject" — the predicate every runs-for-a-subject
    # query composes, reading the attributes stamped at start(). A fresh alias
    # per call keeps it independent of the state/updated_at subqueries, which
    # claim the bare workflow_status table via correlate_except.
    ws = workflow_status.alias()
    return (
        sa.select(ws.c.workflow_uuid)
        .where(
            ws.c.workflow_uuid == run_id,
            ws.c.attributes["subject_type"].as_string() == subject_type,
            ws.c.attributes["subject_id"].as_string() == subject_id,
        )
        .exists()
    )


def state_expression(
    run_id: sa.ColumnElement, input_gate: sa.ColumnElement, created_at: sa.ColumnElement
) -> sa.ColumnElement:
    # The run's state IS the DBOS workflow's, projected onto our vocabulary; the
    # one fact DBOS can't know — parked on a gate — splits PENDING. A NULL status
    # means enqueued, not yet running: scheduled. ELSE keeps a future DBOS status
    # from crashing reads; it reads as running until mapped.
    status = workflow_status.c.status
    mapped = (
        sa.select(
            sa.case(
                (
                    status.is_(None) | status.in_(("ENQUEUED", "DELAYED")),
                    RunState.SCHEDULED.value,
                ),
                ((status == "PENDING") & input_gate.is_not(None), RunState.PENDING_INPUT.value),
                (status == "PENDING", RunState.RUNNING.value),
                (status == "SUCCESS", RunState.FINISHED.value),
                (
                    status.in_(("ERROR", "MAX_RECOVERY_ATTEMPTS_EXCEEDED")),
                    RunState.FAILED.value,
                ),
                (status == "CANCELLED", RunState.CANCELLED.value),
                else_=RunState.RUNNING.value,
            )
        )
        .where(workflow_status.c.workflow_uuid == run_id)
        .correlate_except(workflow_status)
        .scalar_subquery()
    )
    # No workflow row at all: inside the start-gap it's a scheduled run mid-enqueue;
    # past the grace window the row is gone for good, so the run is orphaned — it
    # will never start, rather than reading scheduled forever.
    missing = sa.case(
        (created_at < sa.func.now() - _MISSING_STATUS_GRACE, RunState.ORPHANED.value),
        else_=RunState.SCHEDULED.value,
    )
    return sa.func.coalesce(mapped, missing)


def account_id_expression(run_id: sa.ColumnElement) -> sa.ColumnElement:
    # The account the run was started for — a DBOS attribute like the subject
    # keys, never a durable_runs column. NULL for legacy and actor-less runs.
    return (
        sa.select(workflow_status.c.attributes["account_id"].as_string())
        .where(workflow_status.c.workflow_uuid == run_id)
        .correlate_except(workflow_status)
        .scalar_subquery()
    )


def updated_at_expression(run_id: sa.ColumnElement) -> sa.ColumnElement:
    # DBOS stamps updated_at in epoch milliseconds; convert so it compares
    # against our timestamptz columns.
    return (
        sa.select(sa.func.to_timestamp(workflow_status.c.updated_at / 1000.0))
        .where(workflow_status.c.workflow_uuid == run_id)
        .correlate_except(workflow_status)
        .scalar_subquery()
    )
