"""Tools-limited tokens, generated project prefixes, and ticket cascades.

Revision ID: 9c4e7a1b5d28
Revises: 6f4fe68ac2a2
Create Date: 2026-09-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "9c4e7a1b5d28"
down_revision: str | Sequence[str] | None = "6f4fe68ac2a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _derive_prefix(name: str, taken: set[str]) -> str:
    # Project.derive_prefix, pinned here: two letters of the name plus one more.
    letters = "".join(char for char in name.upper() if char.isascii() and char.isalpha())
    stem = letters[:2]
    for candidate in [stem + extra for extra in letters[2:]] or [stem]:
        if len(candidate) >= 2 and candidate not in taken:
            return candidate
    raise RuntimeError(f"no free ticket prefix for project {name!r}; rename it first")


def upgrade() -> None:
    op.add_column(
        "personal_access_tokens",
        sa.Column("allowed_tools", postgresql.JSONB(), nullable=True),
    )

    bind = op.get_bind()
    taken = set(bind.scalars(sa.text("SELECT prefix FROM projects WHERE prefix IS NOT NULL")))
    rows = bind.execute(sa.text("SELECT id, name FROM projects WHERE prefix IS NULL ORDER BY id"))
    for project_id, name in rows:
        prefix = _derive_prefix(name, taken)
        taken.add(prefix)
        bind.execute(
            sa.text("UPDATE projects SET prefix = :prefix WHERE id = :id"),
            {"prefix": prefix, "id": project_id},
        )
    op.alter_column("projects", "prefix", nullable=False)
    op.drop_constraint("projects_prefix_shape", "projects", type_="check")

    op.drop_constraint("issues_tickets_repo_id_fkey", "issues_tickets", type_="foreignkey")
    op.create_foreign_key(
        "issues_tickets_repo_id_fkey",
        "issues_tickets",
        "project_repos",
        ["repo_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.drop_constraint("issues_comments_ticket_id_fkey", "issues_comments", type_="foreignkey")
    op.create_foreign_key(
        "issues_comments_ticket_id_fkey",
        "issues_comments",
        "issues_tickets",
        ["ticket_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    raise NotImplementedError("Generated prefixes and tools-limited tokens stay.")
