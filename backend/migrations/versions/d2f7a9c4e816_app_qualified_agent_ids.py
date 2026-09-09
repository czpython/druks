"""An agent override and a recorded agent step key on the app-qualified agent id.

Revision ID: d2f7a9c4e816
Revises: b4d7e2a9c1f6
Create Date: 2026-09-06
"""

import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d2f7a9c4e816"
down_revision: str | Sequence[str] | None = "b4d7e2a9c1f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KEYS = ("agent_harness", "agent_model", "agent_billing", "agent_effort", "agent_timeout")
# The bundled app's agents at this revision. The database does not record which
# app declares an agent, so an override or a recorded step of any other app's
# agent keeps its flat name; the operator sets the override again in Settings → Apps.
_AGENTS = (
    "generate_plan",
    "review_plan",
    "revise_contract",
    "implement",
    "evaluate_implementation",
    "triage_human_feedback",
    "repo_profiler",
    "review_pull_request",
)


def _rename(old: str, new: str) -> None:
    """Move the bundled agents' names from the ``old`` prefix to the ``new`` one."""
    op.execute(
        sa.text(
            "UPDATE settings_overrides SET key = split_part(key, ':', 1) || ':' || :new "
            "|| substr(split_part(key, ':', 2), CAST(:cut AS integer)) "
            "WHERE split_part(key, ':', 1) IN :keys AND split_part(key, ':', 2) IN :names"
        ).bindparams(
            sa.bindparam("new", value=new),
            sa.bindparam("cut", value=len(old) + 1),
            sa.bindparam("keys", expanding=True, value=list(_KEYS)),
            sa.bindparam("names", expanding=True, value=[old + agent for agent in _AGENTS]),
        )
    )
    # DBOS replays a retried run's steps under their recorded names, so they
    # follow the id. A fresh install migrates before DBOS creates its schema.
    if op.get_bind().execute(sa.text("SELECT to_regclass('dbos.operation_outputs')")).scalar():
        pattern = r"\.agent\." + re.escape(old) + "(" + "|".join(_AGENTS) + r")(\.retry_wait)?$"
        op.execute(
            sa.text(
                "UPDATE dbos.operation_outputs "
                "SET function_name = regexp_replace(function_name, :pattern, :replacement) "
                "WHERE function_name ~ :pattern"
            ).bindparams(pattern=pattern, replacement=rf".agent.{new}\1\2")
        )


def upgrade() -> None:
    _rename("", "software_factory.")


def downgrade() -> None:
    _rename("software_factory.", "")
