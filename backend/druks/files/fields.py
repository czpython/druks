import json
from typing import Any

from sqlalchemy import ForeignKey, String, cast, func, select, type_coerce
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from druks.files.datastructures import File
from druks.files.models import FileRecord


class _FileColumn(TypeDecorator[File]):
    impl = String
    cache_ok = True

    def column_expression(self, column):
        files = FileRecord.__table__
        metadata = (
            select(
                cast(
                    func.json_build_object(
                        "id",
                        files.c.id,
                        "name",
                        files.c.name,
                        "size",
                        files.c.size,
                        "content_type",
                        files.c.content_type,
                    ),
                    String,
                )
            )
            .where(files.c.id == column)
            .scalar_subquery()
        )
        return type_coerce(metadata, self)

    def process_bind_param(self, value: File | None, dialect: Any) -> str:
        if type(value) is not File or not value.id:
            raise ValueError("FileField takes a hydrated File")
        return value.id

    def process_result_value(self, value: Any, dialect: Any) -> File:
        fields = json.loads(value)
        return File(
            id=fields["id"],
            name=fields["name"],
            size=fields["size"],
            content_type=fields["content_type"],
        )


def FileField() -> Mapped[File]:
    return mapped_column(
        _FileColumn(),
        ForeignKey("files.id", ondelete="RESTRICT"),
        nullable=False,
    )
