from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.orm import Mapped, mapped_column

from druks.core.models import Uuid7Pk
from druks.database import db_session
from druks.models import Base


class Account(Base, Uuid7Pk):
    __tablename__ = "accounts"

    # citext: the column compares and enforces uniqueness case-insensitively,
    # so a lookup or a duplicate check needs no normalization — the email is
    # stored as the provider gave it and matched regardless of case.
    email: Mapped[str] = mapped_column(CITEXT, unique=True)
    # No updated_at: an account is insert-once — email never changes and there
    # is no other field to mutate — so the column would only ever equal
    # created_at, and nothing reads it.
    created_at: Mapped[datetime] = mapped_column(default=Base.utc_now)

    @classmethod
    def get_for_email(cls, email: str) -> "Account | None":
        return db_session().scalar(select(cls).where(cls.email == email))

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
