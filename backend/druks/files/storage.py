import os
import tempfile
import time
from pathlib import Path

from sqlalchemy import select

from druks.database import db_session
from druks.files.constants import REAPER_GRACE_PERIOD
from druks.files.models import FileRecord
from druks.models import Base
from druks.settings import load_settings


class LocalFileStorage:
    def __init__(self, root: Path) -> None:
        self.root = root

    def new_temp(self, file_id: str) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        cutoff = time.time() - REAPER_GRACE_PERIOD.total_seconds()
        for path in self.root.glob(".*.tmp"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except FileNotFoundError:
                pass
        descriptor, name = tempfile.mkstemp(prefix=f".{file_id}.", suffix=".tmp", dir=self.root)
        os.close(descriptor)
        return Path(name)

    def save(self, source: Path, file_id: str) -> Path:
        destination = self.path(file_id)
        source.replace(destination)
        return destination

    def open(self, file_id: str) -> bytes:
        return self.path(file_id).read_bytes()

    def delete(self, file_id: str) -> None:
        self.path(file_id).unlink(missing_ok=True)

    def discard(self, path: Path) -> None:
        path.unlink(missing_ok=True)

    def path(self, file_id: str) -> Path:
        return self.root / file_id


def get_file_storage() -> LocalFileStorage:
    return LocalFileStorage(load_settings().files_dir)


async def reap_deleted_file_bytes() -> int:
    cutoff = Base.utc_now() - REAPER_GRACE_PERIOD
    reaped = list(
        await db_session().scalars(select(FileRecord.id).where(FileRecord.deleted_at <= cutoff))
    )
    storage = get_file_storage()
    for file_id in reaped:
        storage.delete(file_id)
    return len(reaped)
