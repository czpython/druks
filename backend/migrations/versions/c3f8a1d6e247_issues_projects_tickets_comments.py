"""Software Factory local board: projects, tickets, and comments.

Revision ID: c3f8a1d6e247
Revises: d2f7a9c4e816
Create Date: 2026-09-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3f8a1d6e247"
down_revision: str | Sequence[str] | None = "d2f7a9c4e816"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "issues_projects",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("prefix", sa.String(length=6), nullable=False),
        sa.Column("ticket_seq", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("prefix ~ '^[A-Z]{2,6}$'", name="issues_projects_prefix_shape"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
        sa.UniqueConstraint("prefix"),
    )
    op.create_table(
        "issues_tickets",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("identifier", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("priority", sa.String(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("assignee_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["issues_projects.id"]),
        sa.ForeignKeyConstraint(["assignee_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("identifier"),
    )
    op.create_table(
        "issues_comments",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("ticket_id", sa.Integer(), nullable=False),
        sa.Column("author_id", sa.String(), nullable=False),
        sa.Column("body", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["ticket_id"], ["issues_tickets.id"]),
        sa.ForeignKeyConstraint(["author_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("issues_comments")
    op.drop_table("issues_tickets")
    op.drop_table("issues_projects")
