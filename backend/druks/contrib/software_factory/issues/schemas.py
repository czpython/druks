from datetime import datetime

from pydantic import BaseModel, field_validator

from druks.contrib.software_factory.issues.enums import Priority, Status
from druks.workflows import SubjectSummary


class TicketSummary(SubjectSummary):
    # The ticket's domain header. ``label`` is the identifier
    # (``Ticket.get_label``), and the platform's subject read-side composes
    # this with the generic status and timeline.
    title: str
    status: Status


class CommentRead(BaseModel):
    """One line on a thread. ``author`` is the account that wrote it, spelled
    the only way this appliance names a person — the username — and None only
    when that account is gone: a thread still reads without its author."""

    id: int
    author: str | None
    body: str
    created_at: datetime


class TicketDetail(BaseModel):
    """Everything a caller needs to answer a ticket: its description and its
    thread, oldest comment first. The board's summary (``TicketSummary``) is
    the header; this is the ticket itself."""

    identifier: str
    title: str
    description: str
    status: Status
    priority: Priority
    repo_id: int
    owner_id: str | None
    comments: list[CommentRead]


class TicketEdit(BaseModel):
    """A partial edit — what a caller leaves out stays as it was. Status is not
    here: moving a ticket is ``set_status``'s job, the one door that publishes
    ``ticket.transitioned``. A null ``owner_id`` is the one null that says
    something: it clears the owner."""

    title: str | None = None
    description: str | None = None
    priority: Priority | None = None
    owner_id: str | None = None
    repo_id: int | None = None

    @field_validator("owner_id", mode="before")
    @classmethod
    def _blank_is_nobody(cls, value: str | None) -> str | None:
        # An owner select with nobody picked submits "", and the shell sends
        # every field the form shows. Blank means unowned, not an account id to
        # look up — the field still counts as given, so it still clears.
        return value or None
