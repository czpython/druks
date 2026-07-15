from datetime import datetime

from sqlalchemy import String, select
from sqlalchemy.orm import Mapped, mapped_column

from druks.core.models import Uuid7Pk
from druks.database import db_session
from druks.models import Base


class Account(Base, Uuid7Pk):
    __tablename__ = "accounts"

    email: Mapped[str] = mapped_column(String, unique=True)
    created_at: Mapped[datetime] = mapped_column(default=Base.utc_now)
    updated_at: Mapped[datetime] = mapped_column(default=Base.utc_now, onupdate=Base.utc_now)

    @staticmethod
    def normalize_email(value: str) -> str:
        return value.strip().lower()

    @classmethod
    def get_by_email(cls, email: str) -> "Account | None":
        return db_session().scalar(select(cls).where(cls.email == cls.normalize_email(email)))

    @classmethod
    def get_or_create(cls, email: str) -> "Account":
        account = cls.get_by_email(email)
        if account:
            return account
        account = cls(email=cls.normalize_email(email))
        session = db_session()
        session.add(account)
        session.flush()
        return account
