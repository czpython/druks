from datetime import UTC, datetime
from typing import Any, ClassVar

from fastapi import HTTPException, status
from fastapi.responses import JSONResponse, Response

from druks.apps.fetcher import evict_files, evict_repo
from druks.core.services import Github
from druks.services.exceptions import ServiceNotConnectedError
from druks.signals import publish
from druks.webhooks import Webhook, verify_hmac_sha256

_REVIEW_ACTION = {"APPROVED": "approve", "CHANGES_REQUESTED": "request_changes"}

# GitHub's standing for the commenter on that repo. These three write to it;
# everyone else is a passer-by, and on a public repo that is the whole internet.
_WRITERS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})


class GitHubEvents(Webhook):
    """Verifies the GitHub HMAC, then publishes each event as a normalized fact.
    It knows nothing about work items."""

    provider = "github"
    category = "events"

    SIGNATURE_HEADER: ClassVar[str] = "x-hub-signature-256"
    EVENT_HEADER: ClassVar[str] = "x-github-event"
    DELIVERY_HEADER: ClassVar[str] = "x-github-delivery"

    async def request_is_authentic(self) -> bool:
        # The delivery secret lives on the GitHub service-identity row — the
        # same paste that connected the App. No identity, no secret to verify
        # against: reject before any event dispatch.
        try:
            identity = await Github.get()
        except ServiceNotConnectedError as error:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "GitHub is not connected — connect it in Settings → Connections → Services.",
            ) from error
        verify_hmac_sha256(
            self.raw_body,
            self.request.headers.get(self.SIGNATURE_HEADER),
            identity.secrets["webhook_secret"],
        )
        return True

    def get_delivery_key(self) -> str:
        return self.request.headers[self.DELIVERY_HEADER]

    def get_action(self) -> str:
        event = self.request.headers[self.EVENT_HEADER]
        action = self.data.get("action")
        return f"{event}_{action}" if action else event

    async def on_pull_request_review_submitted(self) -> Response:
        sender = self.data["sender"]
        review, pull_request = self.data["review"], self.data["pull_request"]
        action = _REVIEW_ACTION.get(review["state"].upper())
        if sender["type"] == "User" and action:
            await publish(
                "pr.review_submitted",
                repo=_repo_name(self.data),
                pr_number=pull_request["number"],
                payload={
                    "branch": pull_request["head"]["ref"],
                    "action": action,
                    "reviewer": sender["login"],
                    "author_can_write": review["author_association"] in _WRITERS,
                    "body": review["body"] or "",  # body is nullable on an approve
                },
            )
        return _accepted()

    async def on_issue_comment_created(self) -> Response:
        # GitHub files pull-request comments under issues; only those carry ``pull_request``.
        issue = self.data["issue"]
        await self._publish_comment(issue["number"], is_pull_request="pull_request" in issue)
        return _accepted()

    async def on_pull_request_review_comment_created(self) -> Response:
        comment = self.data["comment"]
        await self._publish_comment(
            self.data["pull_request"]["number"],
            is_pull_request=True,
            review_thread_id=comment.get("in_reply_to_id") or comment["id"],
        )
        return _accepted()

    async def _publish_comment(
        self, number: int, *, is_pull_request: bool, review_thread_id: int | None = None
    ) -> None:
        """Publish a person's comment. An inline comment names the review comment GitHub
        takes replies under. A non-User sender is an app talking to itself."""
        sender, comment = self.data["sender"], self.data["comment"]
        if sender["type"] == "User":
            await publish(
                "issue.commented",
                repo=_repo_name(self.data),
                number=number,
                payload={
                    "author": sender["login"],
                    "author_id": sender["id"],
                    "author_can_write": comment["author_association"] in _WRITERS,
                    "body": comment["body"],
                    "comment_id": comment["id"],
                    "review_thread_id": review_thread_id,
                    "is_pull_request": is_pull_request,
                },
            )

    async def on_issues_labeled(self) -> Response:
        """Publish a label that a person or this App put on an issue. Another App's
        label acts for no one here."""
        sender, issue = self.data["sender"], self.data["issue"]
        # GitHub names an App's bot user after the App's slug.
        own_bot = f"{(await Github.get()).identity['slug']}[bot]"
        if sender["type"] == "User" or sender["login"] == own_bot:
            assignee = issue.get("assignee") or {}
            await publish(
                "issue.labeled",
                repo=_repo_name(self.data),
                number=issue["number"],
                payload={
                    "label": self.data["label"]["name"],
                    "actor_id": sender["id"],
                    "title": issue["title"],
                    "url": issue["html_url"],
                    "assignee": assignee.get("login"),
                    "assignee_id": assignee.get("id"),
                },
            )
        return _accepted()

    async def on_pull_request_opened(self) -> Response:
        return await self._publish_pull_request("pr.opened")

    async def on_pull_request_synchronize(self) -> Response:
        return await self._publish_pull_request("pr.synchronized")

    async def on_pull_request_edited(self) -> Response:
        return await self._publish_pull_request("pr.edited")

    async def on_pull_request_ready_for_review(self) -> Response:
        return await self._publish_pull_request("pr.ready_for_review")

    async def _publish_pull_request(self, fact: str) -> Response:
        pull_request = self.data["pull_request"]
        repo = _repo_name(self.data)
        head_repo = pull_request["head"]["repo"]
        # GitHub nulls the head repo after the fork is deleted.
        is_fork = not head_repo or head_repo["full_name"] != repo
        await publish(
            fact,
            repo=repo,
            pr_number=pull_request["number"],
            payload={
                "branch": pull_request["head"]["ref"],
                "is_fork": is_fork,
                "base_branch": pull_request["base"]["ref"],
                "head_sha": pull_request["head"]["sha"],
                "title": pull_request["title"],
                "body": pull_request["body"] or "",
                "draft": pull_request["draft"],
                "author": pull_request["user"]["login"],
                "author_can_write": pull_request["author_association"] in _WRITERS,
            },
        )
        return _accepted()

    async def on_pull_request_closed(self) -> Response:
        pull_request = self.data["pull_request"]
        merged = pull_request["merged"]
        # GitHub's own clock for the verdict, so a redelivered or backfilled close
        # sorts where it happened. A payload missing it leaves receipt time.
        announced = pull_request["merged_at"] if merged else pull_request["closed_at"]
        resolved_at = datetime.fromisoformat(announced) if announced else datetime.now(UTC)
        await publish(
            "pr.closed",
            repo=_repo_name(self.data),
            pr_number=pull_request["number"],
            payload={
                "branch": pull_request["head"]["ref"],
                "merged": merged,
                "resolved_at": resolved_at,
            },
        )
        return _accepted()

    async def on_push(self) -> Response:
        repository = self.data["repository"]
        repo = repository["full_name"]
        to_default_branch = self.data["ref"] == f"refs/heads/{repository['default_branch']}"
        paths = sorted(
            {
                path
                for commit in self.data["commits"]
                for changeset in (commit["added"], commit["removed"], commit["modified"])
                for path in changeset
            }
        )
        if to_default_branch:
            # Before the announcement, so no subscriber reads an old cached copy. A
            # force-push can rewind the branch without listing the files it changed.
            if self.data["forced"]:
                evict_repo(repo=repo)
            else:
                evict_files(repo=repo, paths=paths)
        # Normalized facts only — which paths matter is each subscriber's call.
        await publish("repo.pushed", repo=repo, to_default_branch=to_default_branch, paths=paths)
        return _accepted()


def _accepted() -> Response:
    return JSONResponse({"accepted": True})


def _repo_name(payload: dict[str, Any]) -> str:
    return payload["repository"]["full_name"]
