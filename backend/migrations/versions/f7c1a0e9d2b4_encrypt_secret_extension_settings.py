from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "f7c1a0e9d2b4"
down_revision: str | Sequence[str] | None = "e6f1a2b3c4d5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "settings_overrides",
        sa.Column(
            "secret_value",
            sa.LargeBinary(),
            server_default=sa.text("''::bytea"),
            nullable=False,
        ),
    )
    op.alter_column(
        "settings_overrides",
        "value",
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "settings_overrides",
        "value",
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        nullable=False,
    )
    op.drop_column("settings_overrides", "secret_value")
