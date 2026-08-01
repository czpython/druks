"""usage scrapes keep weekly windows

Revision ID: b6d9e2c4f8a1
Revises: a4b7c2e91d63
Create Date: 2026-08-01 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b6d9e2c4f8a1"
down_revision: str | Sequence[str] | None = "a4b7c2e91d63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "usage_scrapes",
        sa.Column(
            "weeks",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.execute(
        sa.text(
            """
            UPDATE usage_scrapes
            SET weeks = jsonb_build_array(
                jsonb_build_object(
                    'percent_left', week_percent_left,
                    'resets_at', week_resets_at,
                    'model', week_model
                )
            )
            WHERE week_percent_left IS NOT NULL OR week_resets_at IS NOT NULL
            """
        )
    )
    op.drop_column("usage_scrapes", "week_model")
    op.drop_column("usage_scrapes", "week_resets_at")
    op.drop_column("usage_scrapes", "week_percent_left")


def downgrade() -> None:
    op.add_column("usage_scrapes", sa.Column("week_percent_left", sa.Integer(), nullable=True))
    op.add_column(
        "usage_scrapes",
        sa.Column("week_resets_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("usage_scrapes", sa.Column("week_model", sa.String(), nullable=True))
    op.execute(
        sa.text(
            """
            UPDATE usage_scrapes
            SET week_percent_left = (weeks -> 0 ->> 'percent_left')::integer,
                week_resets_at = (weeks -> 0 ->> 'resets_at')::timestamptz,
                week_model = weeks -> 0 ->> 'model'
            WHERE jsonb_array_length(weeks) > 0
            """
        )
    )
    op.drop_column("usage_scrapes", "weeks")
