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
    engine rotates the refresh token on mint; nothing else writes here."""

    __tablename__ = "oauth_connections"

    provider: Mapped[str] = mapped_column(String)
    # Who in druks connected it — every read scopes through the owner.
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="RESTRICT"))
    # Ciphertext at rest; decrypted only into the refresh request body.
    refresh_token = EncryptedTextField()
    # The token response's ``scope`` when the provider echoes one, else the
    # scopes the consent asked for.
    scopes: Mapped[list[str]] = mapped_column(JSONB, default=list)
    connected_at: Mapped[datetime] = mapped_column(default=Base.utc_now)

    @classmethod
    def get(cls, connection_id: str) -> "OauthConnection | None":
        return db_session().get(cls, connection_id)

    @classmethod
    def create(
        cls, *, provider: str, account_id: str, refresh_token: str, scopes: list[str]
    ) -> "OauthConnection":
        connection = cls(
            provider=provider, account_id=account_id, refresh_token=refresh_token, scopes=scopes
        )
        db_session().add(connection)
        db_session().flush()
        return connection

    @classmethod
    def list_for_account(cls, provider: str, account_id: str) -> "list[OauthConnection]":
        return list(
            db_session().scalars(
                select(cls)
                .where(cls.provider == provider, cls.account_id == account_id)
                .order_by(cls.connected_at)
            )
        )

    @classmethod
    def list_for_provider(cls, provider: str) -> "list[OauthConnection]":
        return list(db_session().scalars(select(cls).where(cls.provider == provider)))

    @classmethod
    def list_owned_by(cls, account_id: str | None) -> "list[OauthConnection]":
        return list(
            db_session().scalars(
                select(cls).where(cls.account_id == account_id).order_by(cls.connected_at)
            )
        )

    def reconnect(self, *, refresh_token: str, scopes: list[str]) -> None:
        self.refresh_token = refresh_token
        self.scopes = scopes
        self.connected_at = Base.utc_now()
        db_session().flush()

    def delete(self) -> None:
        session = db_session()
        session.delete(self)
        session.flush()

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
            .one_or_none()
        )
        if fresh:
            return fresh.refresh_token.decrypt()
        raise OauthRefreshError(self.provider, "the connection was removed mid-refresh")

    def _save_refresh_token(self, rotated: str) -> None:
        # The provider invalidated the old token the moment it rotated, so
        # the write commits on its own session, never the enclosing
        # transaction — a step that rolls back later must not brick the
        # connection.
        with get_session(db_session().get_bind()) as session:
            session.execute(
                update(OauthConnection)
                .where(OauthConnection.id == self.id)
                .values(refresh_token=rotated)
            )
            session.commit()
        # Keep the enclosing transaction's copy true as well.
        self.refresh_token = rotated
        db_session().flush()
