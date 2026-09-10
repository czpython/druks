"""Move personal preferences out of execution settings.

Revision ID: 7dc609a2d51a
Revises: 74b53981122c
"""

from alembic import op

revision = "7dc609a2d51a"
down_revision = "74b53981122c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        UPDATE accounts AS account
        SET timezone = COALESCE(
                (SELECT timezone FROM settings WHERE account_id = account.id),
                (SELECT timezone FROM settings WHERE account_id IS NULL),
                'UTC'
            ),
            gate_park_destination_id = CASE
                WHEN EXISTS (SELECT 1 FROM settings WHERE account_id = account.id)
                THEN (SELECT gate_park_destination_id FROM settings WHERE account_id = account.id)
                ELSE (SELECT gate_park_destination_id FROM settings WHERE account_id IS NULL)
            END
    """)
    op.execute("DELETE FROM settings WHERE account_id IS NOT NULL")
    op.execute("UPDATE settings SET id = 1")


def downgrade() -> None:
    raise NotImplementedError("Personal execution defaults cannot be restored.")
