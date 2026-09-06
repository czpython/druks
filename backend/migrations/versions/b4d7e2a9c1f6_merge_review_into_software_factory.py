"""Merge the review app into software_factory.

Revision ID: b4d7e2a9c1f6
Revises: c8b1f4d2a963
Create Date: 2026-09-06
"""

from collections.abc import Sequence

from alembic import op

revision: str = "b4d7e2a9c1f6"
down_revision: str | Sequence[str] | None = "c8b1f4d2a963"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "UPDATE durable_runs SET kind = 'software_factory.pull_request_review' "
        "WHERE kind = 'review.pull_request_review'"
    )
    op.execute("UPDATE events SET app = 'software_factory' WHERE app = 'review'")
    op.execute(
        "UPDATE events SET payload = "
        "jsonb_set(payload, '{kind}', '\"software_factory.pull_request_review\"') "
        "WHERE payload->>'kind' = 'review.pull_request_review'"
    )
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
    op.execute(
        "UPDATE events SET payload = "
        "jsonb_set(payload, '{kind}', '\"review.pull_request_review\"') "
        "WHERE payload->>'kind' = 'software_factory.pull_request_review'"
    )
    op.execute("UPDATE events SET app = 'review' WHERE app = 'software_factory'")
    op.execute(
        "UPDATE durable_runs SET kind = 'review.pull_request_review' "
        "WHERE kind = 'software_factory.pull_request_review'"
    )
