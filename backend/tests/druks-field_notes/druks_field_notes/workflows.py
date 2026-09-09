from druks.sandbox import Sandbox
from druks.workflows import FatalError, Workflow
from druks.workspaces import RepoWorkspace

from druks_field_notes.app import FieldNotes
from druks_field_notes.models import Note, Repository


class Summarize(Workflow):
    """Reads one note and writes its gist — a single durable operation: the agent
    produces the line, and the run stores it on the note."""

    subject = Note
    sandbox = Sandbox(setup="sandboxes/setup.sh")

    async def run(self) -> None:
        note = await self.subject
        # The note body is the agent's prompt context; the gist it returns is the
        # app's own domain result, saved onto the note.
        result = await FieldNotes.summarize(note_body=note.body)
        await note.save_gist(result.gist)

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


class ApproveGist(Workflow):
    """Prepare a gist and ask the operator to approve it."""

    subject = Note

    async def run_multistep(self) -> None:
        await FieldNotes.summarize(note_body=(await self.subject).body)
        reply = await self.review()
        if reply.action != "approve":
            raise FatalError("The operator did not approve the gist.")
        await self.announce("gist.approved")
