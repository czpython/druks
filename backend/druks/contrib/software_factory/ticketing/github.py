import httpx

from druks.core.apis.exceptions import UnknownTicketError
from druks.core.apis.github import GitHubClient
from druks.core.services import Github

from .base import Tracker
from .enums import TicketStatus


def split_key(key: str) -> tuple[str, int]:
    """``owner/repo#123`` -> ``("owner/repo", 123)``.

    GitHub issue numbers are only unique within a repository, so the repo has to
    travel in the key itself. Linear and Jira keys are globally unique in their
    workspace and need no such qualifier.
    """
    repo, _, number = key.partition("#")
    if not repo or not number.isdigit():
        raise UnknownTicketError(key, "GitHub")
    return repo, int(number)


class GitHub(Tracker):
    """Drives a GitHub issue through the build funnel using labels.

    GitHub has no status field, so each configured status name is a label name.
    The labels behave as an exclusive group: setting one removes the others.
    That exclusivity is what makes them act like the status column Linear and
    Jira give us for free - and it is why the trigger label comes off as soon as
    a build claims the issue, so a later re-label opens a fresh build.
    """

    known_exceptions = (UnknownTicketError, httpx.HTTPError)

    def __init__(
        self,
        *,
        status_names: dict[TicketStatus, str],
        client: GitHubClient | None = None,
    ) -> None:
        self._client = client
        # An empty name leaves that status unmapped.
        self._status_names = {status: name for status, name in status_names.items() if name}

    async def _gh(self) -> GitHubClient:
        # The operator App is the tracker's identity, resolved lazily so
        # constructing a tracker never needs a connected service.
        if self._client is None:
            self._client = await Github.get_client()
        return self._client

    async def get_account_id(self, user_id: str) -> str | None:
        # No grant issuer vouches for a GitHub login, so it cannot resolve to a
        # Druks account the way a Linear or Jira user id does.
        return None

    async def set_status(self, key: str, status: TicketStatus) -> None:
        label = self._status_names.get(status)
        if not label:
            raise ValueError(f"GitHub has no configured label for {status}")
        repo, number = split_key(key)
        client = await self._gh()
        # Exclusive group: drop every other label this tracker owns before
        # adding the new one, so an issue never reads as two states at once.
        stale = {name for name in self._status_names.values() if name != label}
        await client.replace_issue_labels(repo, number, add=label, remove=stale)
        if status is TicketStatus.DONE:
            # The label alone would leave finished work in the open-issue list,
            # which is not what a GitHub reader expects it to look like.
            await client.set_issue_state(repo, number, state="closed", state_reason="completed")

    async def aclose(self) -> None:
        # A client we were handed belongs to the caller; only close our own.
        return
