from druks.accounts.dependencies import current_account
from druks.accounts.models import Account
from fastapi import APIRouter, Depends, status
from pydantic import BaseModel

from druks_field_notes.models import Note
from druks_field_notes.schemas import NoteView
from druks_field_notes.workflows import Summarize

# Every APIRouter declared here mounts under /api/field_notes.
router = APIRouter(prefix="/notes", tags=["field_notes"])


class WriteNote(BaseModel):
    body: str


@router.get("", response_model=list[NoteView], response_model_by_alias=True)
async def list_notes() -> list[NoteView]:
    return [NoteView.from_note(note) for note in Note.list_recent()]


@router.post("", status_code=status.HTTP_201_CREATED)
async def write_note(
    request: WriteNote, account: Account = Depends(current_account)
) -> dict[str, int]:
    note = Note.create(body=request.body)
    # The signed-in account attributes the run it triggers.
    await Summarize.dispatch(note_id=note.id, account_id=account.id)
    return {"id": note.id}
