from datetime import datetime
from typing import Any

from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from druks.database import db_session
from druks.models import Base
from druks.secrets.fields import EncryptedJsonField
from druks.services.exceptions import ServiceNotConnectedError


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
        identity = db_session().get(cls, service)
        if identity is None:
            raise ServiceNotConnectedError(service)
        return identity

    @classmethod
    def connect(
        cls, service: str, *, identity: dict[str, Any], secrets: dict[str, str]
    ) -> "ServiceIdentity":
        # The caller verifies the credentials against the service first; this
        # trusts what it is given and overwrites whatever was connected.
        row = db_session().get(cls, service)
        if row is None:
            row = cls(service=service)
            db_session().add(row)
        row.identity = identity
        row.secrets = secrets
        row.connected_at = Base.utc_now()
        db_session().flush()
        return row
