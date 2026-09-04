"""issues: projects, tickets, and comments

Revision ID: issues_0001
Revises:
Create Date: 2026-09-02 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# This app owns an independent migration history — its own
# alembic_version_issues table, never linked to core's revisions.
revision = "issues_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "issues_projects",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("prefix", sa.String(length=6), nullable=False),
        # The monotonic ticket sequence, bumped in place when an identifier is
        # minted and never decremented.
        sa.Column("ticket_seq", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("prefix ~ '^[A-Z]{2,6}$'", name="issues_projects_prefix_shape"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
        sa.UniqueConstraint("prefix"),
    )
    op.create_table(
        "issues_tickets",
        # Integer subject key (StoredSubject.id) — serial, matching create_all.
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
