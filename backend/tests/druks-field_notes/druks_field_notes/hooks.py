from druks.workspaces import RepoWorkspace

from druks_field_notes.app import FieldNotes


@FieldNotes.survey.after_agent
async def keep_the_checkout(*, prompt: str, workspace: RepoWorkspace) -> str | None:
    if await workspace.get_diff():
        return "A survey only reads the repository. Undo your changes to the checkout."
