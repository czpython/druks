from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from druks.core.models import Uuid7Pk
from druks.models import Base


class FileRecord(Base, Uuid7Pk):
    __tablename__ = "files"
    __table_args__ = (
        # A file comes from one place. It can outlive that place: a trimmed run
        # takes its calls with it, and the file stays without a source.
        CheckConstraint(
            "num_nonnulls(uploaded_by, agent_call_id) <= 1", name="files_one_source_check"
        ),
        Index("files_deleted_at_idx", "deleted_at"),
    )

    name: Mapped[str] = mapped_column(String)
    size: Mapped[int] = mapped_column(BigInteger)
    content_type: Mapped[str] = mapped_column(String)
    sha256: Mapped[str] = mapped_column(String(64))
    app: Mapped[str] = mapped_column(String)
    uploaded_by: Mapped[str | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT"), default=None
    )
    agent_call_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_calls.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(default=Base.utc_now)
    deleted_at: Mapped[datetime | None] = mapped_column(default=None)
