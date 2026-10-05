"""Merge the review app into software_factory.

Revision ID: b4d7e2a9c1f6
Revises: c8b1f4d2a963
Create Date: 2026-09-06
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "b4d7e2a9c1f6"
down_revision: str | Sequence[str] | None = "c8b1f4d2a963"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_KIND = "review.pull_request_review"
_NEW_KIND = "software_factory.pull_request_review"

_KIND_STATEMENTS = [
    "UPDATE durable_runs SET kind = :new WHERE kind = :old",
    "UPDATE events SET payload = jsonb_set(payload, '{kind}', to_jsonb(CAST(:new AS text))) "
    "WHERE payload->>'kind' = :old",
]
# DBOS registers a workflow under its kind and names each step after it. A
# retry forks the stored run under those names, so they must follow the kind.
_DBOS_NAME_STATEMENTS = [
    "UPDATE dbos.workflow_status SET name = :new WHERE name = :old",
    "UPDATE dbos.operation_outputs SET function_name = :new "
    "|| substr(function_name, length(CAST(:old AS text)) + 1) "
    "WHERE function_name LIKE :old || '.%'",
]


def _rename_kind(old: str, new: str) -> None:
    statements = list(_KIND_STATEMENTS)
    # A fresh install migrates before DBOS creates its schema.
    if op.get_bind().execute(text("SELECT to_regclass('dbos.workflow_status')")).scalar():
        statements += _DBOS_NAME_STATEMENTS
    for statement in statements:
        op.execute(text(statement).bindparams(old=old, new=new))


def upgrade() -> None:
    _rename_kind(_OLD_KIND, _NEW_KIND)
    op.execute("UPDATE events SET app = 'software_factory' WHERE app = 'review'")
    op.execute(
        "UPDATE settings_overrides SET key = 'app:software_factory:review_app_id' "
        "WHERE key = 'app:review:app_id'"
    )
    op.execute(
        "UPDATE settings_overrides SET key = 'app:software_factory:review_private_key' "
        "WHERE key = 'app:review:private_key'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE settings_overrides SET key = 'app:review:private_key' "
        "WHERE key = 'app:software_factory:review_private_key'"
    )
    op.execute(
        "UPDATE settings_overrides SET key = 'app:review:app_id' "
        "WHERE key = 'app:software_factory:review_app_id'"
    )
    _rename_kind(_NEW_KIND, _OLD_KIND)
    # Only the review runs' events go back. Build and profile events stay.
    op.execute(
        text("UPDATE events SET app = 'review' WHERE payload->>'kind' = :old").bindparams(
            old=_OLD_KIND
        )
    )
