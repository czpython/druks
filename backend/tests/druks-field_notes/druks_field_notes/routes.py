from typing import Annotated

from fastapi import APIRouter, Body, status

from druks_field_notes.models import Note
from druks_field_notes.schemas import NoteSummary
from druks_field_notes.workflows import Summarize

# Every APIRouter declared here mounts under /api/field_notes.
router = APIRouter(prefix="/notes")


@router.get("", response_model=list[NoteSummary], response_model_by_alias=True)
async def list_notes() -> list[NoteSummary]:
    return [NoteSummary.from_note(note) for note in Note.list_recent()]


@router.post("", status_code=status.HTTP_201_CREATED)
async def write_note(body: Annotated[str, Body(embed=True)]) -> dict[str, int]:
    note = Note.create(body=body)
    await Summarize.dispatch(note=note)
    return {"id": note.id}
