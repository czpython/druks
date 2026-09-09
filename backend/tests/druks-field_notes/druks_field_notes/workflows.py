from druks.sandbox import Sandbox
from druks.workflows import Workflow, step
from druks.workspaces import RepoWorkspace

from druks_field_notes.app import FieldNotes
from druks_field_notes.models import Note, Repository


class Summarize(Workflow):
    """Reads one note and saves the gist that the operator approves."""

    subject = Note
    sandbox = Sandbox(setup="sandboxes/setup.sh")

    async def run_multistep(self) -> None:
        note = await self.subject
        operator_note = ""
        while True:
            result = await FieldNotes.summarize(note_body=note.body, operator_note=operator_note)
            reply = await self.review()
            if reply.action == "approve":
                break
            operator_note = reply.note
        await self.save_gist(result.gist)
        await self.announce("note.gist_approved")

    @step
    async def save_gist(self, gist: str) -> None:
        note = await self.subject
        await note.save_gist(gist)

    @classmethod
    async def dispatch(cls, *, note: Note) -> str:
        # Launch policy for a note: one run per note, keyed by its subject; the
        # signed-in requester attributes it ambiently.
        return await cls.start(subject=note)


class Survey(Workflow):
    """Reads one repository, cloned into the VM by the workspace, and writes its gist."""

    subject = Repository
    workspace_class = RepoWorkspace

    async def run(self) -> None:
        repository = await self.subject
        result = await FieldNotes.survey()
        await repository.save_gist(result.gist)

    @classmethod
    async def dispatch(cls, *, repository: Repository) -> str:
        return await cls.start(subject=repository)
