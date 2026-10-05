"""Rename the recorded subject label to key."""

from alembic import op
from sqlalchemy import text

revision = "c7e2f4a9b1d3"
down_revision = "8b194c60e72a"
branch_labels = None
depends_on = None

# A fresh install migrates before DBOS creates its schema.
_ATTRIBUTE_STATEMENT = (
    "UPDATE dbos.workflow_status SET attributes = (attributes - :old) "
    "|| jsonb_build_object(:new, attributes -> :old) WHERE attributes ? :old"
)


def _rename(old: str, new: str) -> None:
    op.execute(f"ALTER TABLE events RENAME COLUMN {old} TO {new}")
    if op.get_bind().execute(text("SELECT to_regclass('dbos.workflow_status')")).scalar():
        op.execute(text(_ATTRIBUTE_STATEMENT).bindparams(old=old, new=new))


def upgrade() -> None:
    _rename("subject_label", "subject_key")


def downgrade() -> None:
    _rename("subject_key", "subject_label")
