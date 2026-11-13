"""A board ticket can be blocked by other board tickets."""

import sqlalchemy as sa
from alembic import op

revision = "c9100c395625"
down_revision = "5eb5546782ac"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ticket_blockers",
        sa.Column(
            "ticket_id",
            sa.Integer(),
            sa.ForeignKey("tickets.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "blocker_id",
            sa.Integer(),
            sa.ForeignKey("tickets.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )


def downgrade() -> None:
    op.drop_table("ticket_blockers")
