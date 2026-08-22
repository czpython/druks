from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, String, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from druks.core.models import Uuid7Pk
from druks.database import db_session, get_session
from druks.models import Base
from druks.secrets.fields import EncryptedJsonField, EncryptedTextField
from druks.services.exceptions import OauthRefreshError, ServiceNotConnectedError


class ServiceIdentity(Base):
    """The appliance's own identity at an external service — druks acting as
    itself, not a per-user harness login. Each service carries its own shape:
    non-secret facts in ``identity``, credentials in ``secrets``."""

    __tablename__ = "service_identities"

    service: Mapped[str] = mapped_column(primary_key=True)
    identity: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    secrets = EncryptedJsonField()
    connected_at: Mapped[datetime]

    @classmethod
    def get(cls, service: str) -> "ServiceIdentity":
        if identity := db_session().get(cls, service):
            return identity
        raise ServiceNotConnectedError(service)

    @classmethod
    def connect(
        cls, service: str, *, identity: dict[str, Any], secrets: dict[str, str]
    ) -> "ServiceIdentity":
        # The caller verifies the credentials against the service first; this
        # trusts what it is given and overwrites whatever was connected.
        row = db_session().get(cls, service)
        if not row:
            row = cls(service=service)
            db_session().add(row)
        row.identity = identity
        row.secrets = secrets
        row.connected_at = Base.utc_now()
        db_session().flush()
        return row


class OauthConnection(Base, Uuid7Pk):
    """One signed-in provider account: the durable outcome of an OAuth
    consent, owned by the druks account that completed it. An account can
    hold many per provider — one per mailbox, handle, or workspace. The
    engine rotates the refresh token on mint; nothing else writes here.

    Revoking is a state, never a deletion: the consent happened, and the row
    keeps its owner, identity, scopes, and dates forever. Only the refresh
    token is cleared. ``reconnect`` returns a revoked row to life."""

    __tablename__ = "oauth_connections"

    provider: Mapped[str] = mapped_column(String)
    # Who in druks connected it — every read scopes through the owner.
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="RESTRICT"))
    # Ciphertext at rest; decrypted only into the refresh request body.
    refresh_token = EncryptedTextField()
    # The token response's ``scope`` when the provider echoes one, else the
    # scopes the consent asked for.
    scopes: Mapped[list[str]] = mapped_column(JSONB, default=list)
    # The provider's facts for the sign-in (email, username, name), set at
    # consent; {} when the provider gave none.
    identity: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    connected_at: Mapped[datetime] = mapped_column(default=Base.utc_now)
    # NULL = live. Stamped with what revoked it: "user", "client_replaced",
    # or "server_removed".
    revoked_at: Mapped[datetime | None] = mapped_column(default=None)
    revoked_reason: Mapped[str] = mapped_column(default="")

    @classmethod
    def get(cls, connection_id: str) -> "OauthConnection | None":
        return db_session().get(cls, connection_id)

    @classmethod
    def create(
        cls,
        *,
        provider: str,
        account_id: str,
        refresh_token: str,
        scopes: list[str],
        identity: dict[str, Any] | None = None,
    ) -> "OauthConnection":
        connection = cls(
            provider=provider,
            account_id=account_id,
            refresh_token=refresh_token,
            scopes=scopes,
            identity=identity or {},
        )
        db_session().add(connection)
        db_session().flush()
        return connection

    @classmethod
    def list_for_account(cls, provider: str, account_id: str) -> "list[OauthConnection]":
        return list(
            db_session().scalars(
                select(cls)
                .where(
                    cls.provider == provider,
                    cls.account_id == account_id,
                    cls.revoked_at.is_(None),
                )
                .order_by(cls.connected_at)
            )
        )

    @classmethod
    def get_for_identity(
        cls, provider: str, account_id: str, key: str, value: Any
    ) -> "OauthConnection | None":
        # A live grant outranks revoked history; among revoked, the latest
        # consent.
        return (
            db_session()
            .scalars(
                select(cls)
                .where(
                    cls.provider == provider,
                    cls.account_id == account_id,
                    cls.identity[key].astext == str(value),
                )
                .order_by(cls.revoked_at.is_(None).desc(), cls.connected_at.desc())
                .limit(1)
            )
            .first()
        )

    @classmethod
    def list_for_provider(
        cls, provider: str, *, include_revoked: bool = False
    ) -> "list[OauthConnection]":
        query = select(cls).where(cls.provider == provider)
        if not include_revoked:
            query = query.where(cls.revoked_at.is_(None))
        return list(db_session().scalars(query))

    @classmethod
    def list_owned_by(cls, account_id: str | None) -> "list[OauthConnection]":
        # The audit read: everything this account ever authorized, revoked
        # rows included.
        return list(
            db_session().scalars(
                select(cls).where(cls.account_id == account_id).order_by(cls.connected_at)
            )
        )

    def reconnect(
        self, *, refresh_token: str, scopes: list[str], identity: dict[str, Any] | None = None
    ) -> None:
        self.refresh_token = refresh_token
        self.scopes = scopes
        if identity:
            self.identity = identity
        self.connected_at = Base.utc_now()
        self.revoked_at = None
        self.revoked_reason = ""
        db_session().flush()

    def revoke(self, reason: str) -> None:
        # The consent's facts survive; only the secret is cleared. A second
        # revoke keeps the first stamp.
        self.revoked_at = self.revoked_at or Base.utc_now()
        self.revoked_reason = self.revoked_reason or reason
        self.refresh_token = ""
        db_session().flush()

    def _load_refresh_token(self) -> str:
        # Under the refresh lock: another process may have rotated and
        # committed, and this transaction may already hold the row —
        # populate_existing re-reads it past the identity map.
        fresh = (
            db_session()
            .scalars(
                select(OauthConnection)
                .where(OauthConnection.id == self.id)
                .execution_options(populate_existing=True)
            )
            .one()
        )
        if fresh.revoked_at:
            raise OauthRefreshError(self.provider, "the connection was revoked mid-refresh")
        return fresh.refresh_token.decrypt()

    def _save_refresh_token(self, rotated: str) -> None:
        # The provider invalidated the old token the moment it rotated, so
        # the write commits on its own session, never the enclosing
        # transaction — a step that rolls back later must not brick the
        # connection.
        with get_session(db_session().get_bind()) as session:
            stored = session.execute(
                update(OauthConnection)
                .where(OauthConnection.id == self.id, OauthConnection.revoked_at.is_(None))
                .values(refresh_token=rotated)
            )
            session.commit()
        if not stored.rowcount:
            # A revoke landed mid-refresh. Nothing secret outlives the
            # consent at rest, so the rotated token is not stored.
            raise OauthRefreshError(self.provider, "the connection was revoked mid-refresh")
        # Expire the stale copy instead of assigning it. The next read loads
        # the rotated value, and the enclosing transaction never writes this
        # column at commit — a revoke that lands between the two commits
        # must keep its cleared token.
        db_session().expire(self, ["refresh_token"])
