from datetime import datetime

from sqlalchemy import String, select
from sqlalchemy.orm import Mapped, mapped_column, validates

from druks.core.models import Uuid7Pk
from druks.database import db_session
from druks.models import Base


class Account(Base, Uuid7Pk):
    __tablename__ = "accounts"

    email: Mapped[str] = mapped_column(String, unique=True)
    # No updated_at: an account is insert-once — email never changes and there
    # is no other field to mutate — so the column would only ever equal
    # created_at, and nothing reads it.
    created_at: Mapped[datetime] = mapped_column(default=Base.utc_now)

    @validates("email")
    def _canonical_email(self, _key: str, value: str) -> str:
        # Canonical on write, so the stored value is the one every reader
        # compares, displays, and keys on — no second shape anywhere.
        return value.strip().lower()

    @classmethod
    def get_for_email(cls, email: str) -> "Account | None":
        return db_session().scalar(select(cls).where(cls.email == email.strip().lower()))

    @classmethod
    def get_or_create(cls, email: str) -> "Account":
        account = cls.get_for_email(email)
        if account:
            return account
        account = cls(email=email)
        session = db_session()
        session.add(account)
        session.flush()
        return account
