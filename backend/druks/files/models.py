from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from druks.core.models import Uuid7Pk
from druks.models import Base


class FileRecord(Base, Uuid7Pk):
    __tablename__ = "files"
    __table_args__ = (
        CheckConstraint("origin_type IN ('agent_call')", name="files_origin_type_check"),
        Index("files_deleted_at_idx", "deleted_at"),
    )

    name: Mapped[str] = mapped_column(String)
    size: Mapped[int] = mapped_column(BigInteger)
    content_type: Mapped[str] = mapped_column(String)
    sha256: Mapped[str] = mapped_column(String(64))
    app: Mapped[str] = mapped_column(String)
    origin_type: Mapped[str] = mapped_column(String)
    origin_id: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(default=Base.utc_now)
    deleted_at: Mapped[datetime | None] = mapped_column(default=None)
