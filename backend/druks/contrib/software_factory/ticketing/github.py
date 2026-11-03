from githubkit.exception import GitHubException

from druks.core import services
from druks.core.apis.exceptions import GitHubAppNotInstalledError, UnknownTicketError
from druks.core.apis.github import GITHUB_AUTHORITY

from .base import Tracker
from .enums import TicketStatus


class GitHub(Tracker):
    """GitHub has no status field, so each status name is a label. The status labels
    are one exclusive group: setting one takes the others off, the trigger label too,
    so adding the trigger label again is a new event."""

    known_exceptions = (GitHubException, GitHubAppNotInstalledError, UnknownTicketError)
    authority = GITHUB_AUTHORITY

    def __init__(self, *, status_names: dict[TicketStatus, str]) -> None:
        # An empty name leaves that status unmapped.
        self._status_names = {status: name for status, name in status_names.items() if name}

    async def set_status(self, key: str, status: TicketStatus) -> None:
        name = self._status_names.get(status)
        if not name:
            raise ValueError(f"GitHub has no configured label for {status}")
        # Issue numbers repeat across repositories, so the key is ``owner/repo#number``.
        repo, _, number = key.partition("#")
        if "/" not in repo or not number.isdigit():
            raise UnknownTicketError(key, "GitHub")
        client = await services.Github.get_client(repo)
        others = {label for label in self._status_names.values() if label != name}
        await client.replace_issue_labels(repo, int(number), add=name, remove=others)
        if status is TicketStatus.DONE:
            # Finished work on GitHub is a closed issue, not only a label.
            await client.close_issue(repo, int(number))

    async def aclose(self) -> None:
        return
