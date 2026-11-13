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

    async def get_ticket(self, key: str) -> dict:
        repo, number = self._parse_key(key)
        client = await services.Github.get_client(repo)
        issue = await client.get_issue(repo, number)
        return self._to_ticket(issue)

    def _to_ticket(self, issue: dict) -> dict:
        key = self._to_key(issue)
        repo, _ = self._parse_key(key)
        status = None
        # A closed issue has left every status, whatever labels it keeps.
        if issue["state"] == "open":
            labels = {label["name"] for label in issue["labels"]}
            status = next((name for name in self._status_names.values() if name in labels), None)
        # The client leaves out a null assignee. GitHub's assignee is the first of these.
        assignee = issue["assignees"][0] if issue["assignees"] else {}
        return {
            "source": "github",
            "ticket_id": str(issue["id"]),
            "identifier": key,
            "status": status,
            "title": issue["title"],
            "url": issue["html_url"],
            # The issue's own repo is the build's repo, so no label routes it elsewhere.
            "project_name": repo,
            "labels": [],
            "actor_id": None,
            "assignee_id": str(assignee["id"]) if assignee else None,
            "assignee_email": None,
            "assignee_name": assignee.get("login"),
        }

    async def get_status_name(self, key: str) -> str | None:
        return (await self.get_ticket(key))["status"]

    async def set_status(self, key: str, status: TicketStatus) -> None:
        name = self._status_names.get(status)
        if not name:
            raise ValueError(f"GitHub has no configured label for {status}")
        repo, number = self._parse_key(key)
        client = await services.Github.get_client(repo)
        others = {label for label in self._status_names.values() if label != name}
        await client.replace_issue_labels(repo, number, add=name, remove=others)
        if status is TicketStatus.DONE:
            # Finished work on GitHub is a closed issue, not only a label.
            await client.close_issue(repo, number)

    async def get_blocked_by(self, key: str) -> dict[str, bool]:
        repo, number = self._parse_key(key)
        client = await services.Github.get_client(repo)
        issues = await client.list_blocked_by(repo, number)
        done_label = self._status_names.get(TicketStatus.DONE)
        # The done label's event starts the blocked tickets, and Druks adds the label
        # before it closes the issue.
        return {
            self._to_key(issue): (
                issue["state"] == "open"
                and done_label not in {label["name"] for label in issue["labels"]}
            )
            for issue in issues
        }

    async def get_blocking(self, key: str) -> list[dict]:
        repo, number = self._parse_key(key)
        client = await services.Github.get_client(repo)
        return [self._to_ticket(issue) for issue in await client.list_blocking(repo, number)]

    @staticmethod
    def _to_key(issue: dict) -> str:
        # A dependency can be in another repo. Its API URL ends in ``<owner>/<repo>``.
        repo = "/".join(issue["repository_url"].split("/")[-2:])
        return f"{repo}#{issue['number']}"

    @staticmethod
    def _parse_key(key: str) -> tuple[str, int]:
        # Issue numbers repeat across repositories, so the key is ``owner/repo#number``.
        repo, _, number = key.partition("#")
        if "/" not in repo or not number.isdigit():
            raise UnknownTicketError(key, "GitHub")
        return repo, int(number)

    async def aclose(self) -> None:
        return
