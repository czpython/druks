"""Record event transaction IDs for snapshot cursors."""

from alembic import op

revision = "8b194c60e72a"
down_revision = "83a72f16b940"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE events ADD COLUMN xid xid8 NOT NULL DEFAULT pg_current_xact_id()")
    op.create_index("events_xid_idx", "events", ["xid"])


def downgrade() -> None:
    op.drop_index("events_xid_idx", table_name="events")
    op.drop_column("events", "xid")
