from typing import Annotated

from fastapi import APIRouter, Body, HTTPException, status

from druks_field_notes.models import Note
from druks_field_notes.schemas import NoteSummary
from druks_field_notes.workflows import Summarize

# Every APIRouter declared here mounts under /api/field_notes.
router = APIRouter(prefix="/notes")


@router.get("", response_model=list[NoteSummary], response_model_by_alias=True)
async def list_notes() -> list[NoteSummary]:
    return [note.get_summary() for note in await Note.list_recent()]


@router.post("", status_code=status.HTTP_201_CREATED, operation_id="write_note")
async def write_note(body: Annotated[str, Body(embed=True)]) -> dict[str, int]:
    note = await Note.create(body=body)
    await Summarize.dispatch(note=note)
    return {"id": note.id}


@router.post("/{note_id}/gist", operation_id="clear_gist")
async def clear_gist(note_id: int) -> dict[str, str]:
    note = await Note.get(note_id)
    if not note:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No note {note_id}.")
    await note.save_gist("")
    return {"result": "cleared"}
