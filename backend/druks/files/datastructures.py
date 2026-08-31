import asyncio
import hashlib
from typing import Any

from pydantic import GetCoreSchemaHandler
from pydantic_core import CoreSchema, core_schema

from druks.core.models import uuid7_str
from druks.database import db_session
from druks.files.constants import MAX_FILE_BYTES
from druks.files.exceptions import FileTooLargeError, FileUnavailableError
from druks.files.models import FileRecord
from druks.files.storage import get_file_storage
from druks.models import Base

_FILE_URL_PREFIX = "/api/files/"
_FILE_PATH_DESCRIPTION = "Write the file in your workspace and report its path."


class File:
    def __init__(
        self,
        *,
        id: str = "",
        path: str = "",
        name: str = "",
        size: int = 0,
        content_type: str = "",
    ) -> None:
        self.id = id
        self.path = path
        self.name = name
        self.size = size
        self.content_type = content_type

    @classmethod
    async def create(
        cls,
        *,
        name: str,
        content_type: str,
        content: bytes,
        app: str,
        uploaded_by: str,
    ) -> "File":
        if len(content) > MAX_FILE_BYTES:
            raise FileTooLargeError(f"{name} is {len(content)} bytes; the cap is {MAX_FILE_BYTES}")
        file_id = uuid7_str()
        storage = get_file_storage()
        temp = storage.new_temp(file_id)
        try:
            await asyncio.to_thread(temp.write_bytes, content)
            digest = await asyncio.to_thread(hashlib.sha256, content)
            record = FileRecord(
                id=file_id,
                name=name,
                size=len(content),
                content_type=content_type,
                sha256=digest.hexdigest(),
                app=app,
                uploaded_by=uploaded_by,
            )
            db_session().add(record)
            await db_session().flush()
            storage.save(temp, file_id)
        except BaseException:
            storage.discard(temp)
            storage.delete(file_id)
            raise
        return cls(
            id=record.id, name=record.name, size=record.size, content_type=record.content_type
        )

    @classmethod
    def _validate(cls, value: str, info: core_schema.ValidationInfo) -> "File":
        # Validating a harness result: the caller's list collects every File the
        # contract declares, so hydration never walks the output tree.
        file = cls(path=value)
        if info.context:
            info.context["workspace_files"].append(file)
        return file

    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        source_type: Any,
        handler: GetCoreSchemaHandler,
    ) -> CoreSchema:
        return core_schema.with_info_after_validator_function(
            cls._validate,
            core_schema.str_schema(),
            serialization=core_schema.plain_serializer_function_ser_schema(
                lambda file: file.url,
                when_used="json",
            ),
            metadata={
                "pydantic_js_updates": {
                    "description": _FILE_PATH_DESCRIPTION,
                }
            },
        )

    @property
    def url(self) -> str:
        return f"{_FILE_URL_PREFIX}{self.id}"

    async def open(self) -> bytes:
        record = await db_session().get(FileRecord, self.id)
        if not record or record.deleted_at:
            raise FileUnavailableError(f"file {self.id} is deleted or missing")
        try:
            return await asyncio.to_thread(get_file_storage().open, self.id)
        except FileNotFoundError as error:
            raise FileUnavailableError(f"file {self.id} content is missing") from error

    async def delete(self) -> None:
        record = await db_session().get(FileRecord, self.id)
        if not record:
            raise FileUnavailableError(f"file {self.id} is missing")
        if not record.deleted_at:
            record.deleted_at = Base.utc_now()
            await db_session().flush()

    def _hydrate(self, record: "FileRecord") -> None:
        self.id = record.id
        self.path = ""
        self.name = record.name
        self.size = record.size
        self.content_type = record.content_type

    def __repr__(self) -> str:
        return f"File({self.id or self.path!r})"
