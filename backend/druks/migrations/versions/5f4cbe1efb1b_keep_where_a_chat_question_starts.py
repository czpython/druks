"""A notification keeps where the question of a chat's agent starts in its body, so a cut
to fit a message takes only the reply before it. An index finds the notifications of a
chat, so a person's turn finds its open questions without a scan of every notification."""

import sqlalchemy as sa
from alembic import op

revision = "5f4cbe1efb1b"
down_revision = "b7e2f4a91c36"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("notifications", sa.Column("question_offset", sa.Integer(), nullable=True))
    op.create_index(
        "notifications_conversation_idx",
        "notifications",
        ["conversation_id"],
        postgresql_where=sa.text("conversation_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("notifications_conversation_idx", table_name="notifications")
    op.drop_column("notifications", "question_offset")
