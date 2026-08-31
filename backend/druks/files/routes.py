from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import FileResponse

from druks.database import db_session
from druks.files.constants import INLINE_CONTENT_TYPES
from druks.files.models import FileRecord
from druks.files.storage import get_file_storage

router = APIRouter(prefix="/api/files", tags=["files"])


@router.get("/{file_id}")
async def get_file(file_id: str, request: Request) -> Response:
    record = await db_session().get(FileRecord, file_id)
    if not record or record.deleted_at:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "file not found")
    path = get_file_storage().path(record.id)
    if not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "file content missing")

    # An ETag is quoted on the wire (RFC 9110).
    etag = f'"{record.sha256}"'
    headers = {
        "Cache-Control": "no-cache",
        "ETag": etag,
        "X-Content-Type-Options": "nosniff",
    }
    validators = {
        validator.strip() for validator in request.headers.get("if-none-match", "").split(",")
    }
    if etag in validators or f"W/{etag}" in validators or "*" in validators:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)

    if record.content_type in INLINE_CONTENT_TYPES:
        disposition = "inline"
    else:
        disposition = "attachment"
    return FileResponse(
        path,
        media_type=record.content_type,
        filename=record.name,
        content_disposition_type=disposition,
        headers=headers,
    )
