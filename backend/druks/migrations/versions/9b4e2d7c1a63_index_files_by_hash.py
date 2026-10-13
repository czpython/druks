"""Index files by hash.

Revision ID: 9b4e2d7c1a63
Revises: 7c2d9a41e5b3
"""

from alembic import op

revision = "9b4e2d7c1a63"
down_revision = "7c2d9a41e5b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("files_sha256_idx", "files", ["sha256"])


def downgrade() -> None:
    op.drop_index("files_sha256_idx", table_name="files")
