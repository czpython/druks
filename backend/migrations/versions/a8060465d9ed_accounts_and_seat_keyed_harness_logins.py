"""accounts and seat-keyed harness logins

Revision ID: a8060465d9ed
Revises: 619e34746ef3
Create Date: 2026-07-15 20:10:00.000000

"""

import json
import os
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op
from uuid_utils import uuid7

from druks.secrets import utils as secrets

# revision identifiers, used by Alembic.
revision: str = "a8060465d9ed"
down_revision: str | Sequence[str] | None = "619e34746ef3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The envelope AAD is the FINAL ``table.column`` — encrypting under the
# temporary column's name would brick every ciphertext at the rename.
_PAYLOAD_AAD = "harness_logins.payload"


def upgrade() -> None:
    bind = op.get_bind()
    legacy = (
        bind.execute(sa.text("SELECT harness, payload, account FROM harness_logins"))
        .mappings()
        .all()
    )

    email = ""
    if legacy:
        # Fail before any mutation: existing seats are re-keyed under one
        # account, and only the operator knows which identity that is.
        email = os.environ.get("DRUKS_DASHBOARD_EMAIL", "").strip().lower()
        if not email:
            raise RuntimeError(
                "DRUKS_DASHBOARD_EMAIL must be set (non-blank) for this migration: "
                "existing harness logins are re-keyed under one account with that "
                "email. Export it for this one run, set to the provider email you "
                "will sign in with once account login lands."
            )

    op.create_table(
        "accounts",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
    )

    op.add_column("harness_logins", sa.Column("id", sa.String(), nullable=True))
    op.add_column("harness_logins", sa.Column("account_id", sa.String(), nullable=True))
    op.add_column("harness_logins", sa.Column("provider_email", sa.String(), nullable=True))
    op.add_column("harness_logins", sa.Column("is_default", sa.Boolean(), nullable=True))
    op.add_column(
        "harness_logins", sa.Column("payload_ciphertext", sa.LargeBinary(), nullable=True)
    )

    if legacy:
        now = datetime.now(UTC)
        account_id = str(uuid7())
        bind.execute(
            sa.text(
                "INSERT INTO accounts (id, email, created_at, updated_at) "
                "VALUES (:id, :email, :now, :now)"
            ),
            {"id": account_id, "email": email, "now": now},
        )
        for row in legacy:
            # Canonical serialization matches EncryptedJson's bind exactly, so
            # the decrypt-verify below compares like for like.
            canonical = json.dumps(row["payload"], separators=(",", ":"), sort_keys=True)
            provider_email = (row["account"] or "").strip().lower()
            bind.execute(
                sa.text(
                    "UPDATE harness_logins SET id = :id, account_id = :account_id, "
                    "provider_email = :provider_email, is_default = true, "
                    "payload_ciphertext = :ciphertext WHERE harness = :harness"
                ),
                {
                    "id": str(uuid7()),
                    "account_id": account_id,
                    "provider_email": provider_email or None,
                    "ciphertext": secrets.encrypt(canonical.encode(), _PAYLOAD_AAD),
                    "harness": row["harness"],
                },
            )

    op.drop_constraint("harness_logins_pkey", "harness_logins", type_="primary")
    op.drop_column("harness_logins", "payload")
    op.drop_column("harness_logins", "account")

    op.alter_column("harness_logins", "payload_ciphertext", new_column_name="payload")
    op.alter_column("harness_logins", "id", existing_type=sa.String(), nullable=False)
    op.alter_column("harness_logins", "account_id", existing_type=sa.String(), nullable=False)
    op.alter_column("harness_logins", "payload", existing_type=sa.LargeBinary(), nullable=False)
    op.alter_column(
        "harness_logins",
        "is_default",
        existing_type=sa.Boolean(),
        nullable=False,
        server_default=sa.false(),
    )
    op.create_primary_key("harness_logins_pkey", "harness_logins", ["id"])
    op.create_foreign_key(
        "harness_logins_account_id_fkey",
        "harness_logins",
        "accounts",
        ["account_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "harness_logins_harness_account_id_key", "harness_logins", ["harness", "account_id"]
    )
    op.create_index(
        "harness_logins_provider_email_idx",
        "harness_logins",
        ["harness", "provider_email"],
        unique=True,
        postgresql_where=sa.text("provider_email IS NOT NULL"),
    )
    op.create_index(
        "harness_logins_default_idx",
        "harness_logins",
        ["harness"],
        unique=True,
        postgresql_where=sa.text("is_default"),
    )

    # Decrypt-verify every migrated value under the final AAD before this
    # revision commits — a payload that can't round-trip must abort here, not
    # surface as a dead credential at the next run.
    originals = {row["harness"]: row["payload"] for row in legacy}
    migrated = (
        bind.execute(sa.text("SELECT harness, payload FROM harness_logins")).mappings().all()
    )
    for row in migrated:
        stored = json.loads(secrets.decrypt(bytes(row["payload"]), _PAYLOAD_AAD))
        if stored != originals[row["harness"]]:
            raise RuntimeError(
                f"harness login {row['harness']!r} did not round-trip through encryption"
            )


def downgrade() -> None:
    raise RuntimeError(
        "irreversible: re-keyed, encrypted harness logins cannot be collapsed back "
        "to one plaintext row per harness — restore the pre-migration database "
        "backup and the old image instead"
    )
