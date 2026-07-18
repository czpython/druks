"""rename account email to username

Revision ID: e8f9a0b1c2d3
Revises: ccebfa4ee1e2
Create Date: 2026-07-18 12:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e8f9a0b1c2d3"
down_revision: str | Sequence[str] | None = "ccebfa4ee1e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The value was never only an email: the system account holds "system", and
    # other identity providers may hand usernames. Pure rename — citext,
    # uniqueness, and every row survive; the constraint follows so a migrated
    # database and a fresh create_all converge on one name.
    op.alter_column("accounts", "email", new_column_name="username")
    op.execute(
        "ALTER TABLE accounts RENAME CONSTRAINT accounts_email_key TO accounts_username_key"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE accounts RENAME CONSTRAINT accounts_username_key TO accounts_email_key"
    )
    op.alter_column("accounts", "username", new_column_name="email")
