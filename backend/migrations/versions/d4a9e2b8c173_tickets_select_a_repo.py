"""Tickets select a GitHub repo; prefix lives on Project.

Revision ID: d4a9e2b8c173
Revises: c3f8a1d6e247
Create Date: 2026-09-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4a9e2b8c173"
down_revision: str | Sequence[str] | None = "c3f8a1d6e247"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Unreleased stack: nothing in production to map from IssuesProject onto a
    # repo, so drop the ticket rows rather than invent a routing they never had.
    op.execute(sa.text("DELETE FROM issues_comments"))
    op.execute(sa.text("DELETE FROM issues_tickets"))
    op.drop_constraint("issues_tickets_project_id_fkey", "issues_tickets", type_="foreignkey")
    op.drop_column("issues_tickets", "project_id")
    op.add_column("issues_tickets", sa.Column("repo_id", sa.Integer(), nullable=False))
    op.create_foreign_key(
        "issues_tickets_repo_id_fkey",
        "issues_tickets",
        "project_repos",
        ["repo_id"],
        ["id"],
    )
    op.add_column("issues_tickets", sa.Column("creator_id", sa.String(), nullable=True))
    op.create_foreign_key(
        "issues_tickets_creator_id_fkey",
        "issues_tickets",
        "accounts",
        ["creator_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.drop_table("issues_projects")

    op.add_column("projects", sa.Column("prefix", sa.String(length=6), nullable=True))
    op.add_column(
        "projects",
        sa.Column("ticket_seq", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_unique_constraint("projects_prefix_key", "projects", ["prefix"])
    op.create_check_constraint(
        "projects_prefix_shape",
        "projects",
        "(prefix IS NULL) OR (prefix ~ '^[A-Z]{2,6}$')",
    )


def downgrade() -> None:
    raise NotImplementedError("Ticket-to-repo routing is forward-only.")
