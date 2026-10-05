"""The Druks board: project prefixes, tickets, comments, and scoped tokens.

Revision ID: b1f4c07d9e52
Revises: b43924bf37db
Create Date: 2026-09-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b1f4c07d9e52"
down_revision: str | Sequence[str] | None = "b43924bf37db"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _derive_prefix(name: str, taken: set[str]) -> str:
    # Project.derive_prefix, pinned: the first two letters and one later letter.
    letters = "".join(char for char in name.upper() if char.isascii() and char.isalpha())
    for candidate in [letters[:2] + extra for extra in letters[2:]] or [letters]:
        if len(candidate) >= 2 and candidate not in taken:
            return candidate
    raise RuntimeError(f"project {name!r} has no free ticket prefix. Rename it first.")


def upgrade() -> None:
    op.add_column("projects", sa.Column("prefix", sa.String(length=6), nullable=True))
    op.add_column(
        "projects", sa.Column("ticket_seq", sa.Integer(), nullable=False, server_default="0")
    )
    op.create_unique_constraint("projects_prefix_key", "projects", ["prefix"])
    bind = op.get_bind()
    taken: set[str] = set()
    for project_id, name in bind.execute(sa.text("SELECT id, name FROM projects ORDER BY id")):
        prefix = _derive_prefix(name, taken)
        taken.add(prefix)
        bind.execute(
            sa.text("UPDATE projects SET prefix = :prefix WHERE id = :id"),
            {"prefix": prefix, "id": project_id},
        )
    op.alter_column("projects", "prefix", nullable=False)

    op.create_table(
        "tickets",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("identifier", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("priority", sa.String(), nullable=False),
        sa.Column("repo_id", sa.Integer(), nullable=False),
        sa.Column("owner_id", sa.String(), nullable=True),
        sa.Column("creator_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["repo_id"], ["project_repos.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["owner_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["creator_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("identifier"),
    )
    op.create_table(
        "ticket_comments",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("ticket_id", sa.Integer(), nullable=False),
        sa.Column("author_id", sa.String(), nullable=False),
        sa.Column("body", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["ticket_id"], ["tickets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["author_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.add_column(
        "personal_access_tokens", sa.Column("allowed_tools", postgresql.JSONB(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("personal_access_tokens", "allowed_tools")
    op.drop_table("ticket_comments")
    op.drop_table("tickets")
    op.drop_constraint("projects_prefix_key", "projects", type_="unique")
    op.drop_column("projects", "ticket_seq")
    op.drop_column("projects", "prefix")
