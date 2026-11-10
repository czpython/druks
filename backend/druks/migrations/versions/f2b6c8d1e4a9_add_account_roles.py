"""Add roles to operator accounts.

Revision ID: f2b6c8d1e4a9
Revises: d1f7b3a96c20
"""

import sqlalchemy as sa
from alembic import op

revision: str = "f2b6c8d1e4a9"
down_revision: str | None = "d1f7b3a96c20"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column("role", sa.String(), server_default="member", nullable=True),
    )
    op.execute("UPDATE accounts SET role = CASE WHEN kind = 'operator' THEN 'admin' ELSE NULL END")
    op.create_check_constraint(
        "accounts_role_allowed", "accounts", condition="role IN ('admin', 'member')"
    )


def downgrade() -> None:
    op.drop_constraint("accounts_role_allowed", "accounts", type_="check")
    op.drop_column("accounts", "role")
