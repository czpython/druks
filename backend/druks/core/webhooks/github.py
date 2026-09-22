from datetime import UTC, datetime
from typing import Any, ClassVar

from fastapi import HTTPException, status
from fastapi.responses import JSONResponse, Response

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

    def delivery_key(self) -> str:
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
                    "body": review["body"] or "",  # body is nullable on an approve
                },
            )
        return _accepted()

    async def on_issue_comment_created(self) -> Response:
        # GitHub files pull-request comments under issues; only those carry ``pull_request``.
        issue = self.data["issue"]
        if "pull_request" in issue:
            await self._publish_comment(issue["number"])
        return _accepted()

    async def on_pull_request_review_comment_created(self) -> Response:
        await self._publish_comment(self.data["pull_request"]["number"])
        return _accepted()

    async def _publish_comment(self, pr_number: int) -> None:
        # A non-User sender is an app talking to itself.
        sender, comment = self.data["sender"], self.data["comment"]
        if sender["type"] == "User":
            await publish(
                "pr.commented",
                repo=_repo_name(self.data),
                pr_number=pr_number,
                payload={
                    "author": sender["login"],
                    "author_can_write": comment["author_association"] in _WRITERS,
                    "body": comment["body"],
                },
            )

    async def on_issues_labeled(self) -> Response:
        """A label landing on an issue is GitHub's nearest thing to a status
        transition, so it is published as one.

        No sender filter: the trigger label is the only status a subscriber acts
        on, and druks setting it through the tracker API is the intended way to
        open a build - exactly as moving a Linear ticket is. Every other label
        this app writes carries a non-trigger status, so its echo is inert, and a
        redelivery resolves to the same work item.
        """
        issue, repository = self.data["issue"], self.data["repository"]
        # GitHub issue numbers repeat across repositories, so the repo travels
        # in the identifier. It also makes the key self-routing.
        identifier = f"{repository['full_name']}#{issue['number']}"
        assignee = issue.get("assignee") or {}
        await publish(
            "ticket.transitioned",
            payload={
                "source": "github",
                "identifier": identifier,
                "status": self.data["label"]["name"],
                "title": issue["title"],
                "url": issue["html_url"],
                # The bare repo name routes to a ProjectRepo the same way a
                # Linear project name does.
                "project_name": repository["name"],
                "labels": [label["name"] for label in issue["labels"]],
                # A GitHub webhook carries a login, which is neither an address
                # nor an identity any grant issuer vouches for. Leave both unset
                # rather than guess; the name is for display only.
                "assignee_id": None,
                "assignee_email": None,
                "assignee_name": assignee.get("login"),
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
        # Normalized facts only — which paths matter is each subscriber's call.
        repository = self.data["repository"]
        await publish(
            "repo.pushed",
            repo=repository["full_name"],
            to_default_branch=self.data["ref"] == f"refs/heads/{repository['default_branch']}",
            paths=sorted(
                {
                    path
                    for commit in self.data["commits"]
                    for changeset in (commit["added"], commit["removed"], commit["modified"])
                    for path in changeset
                }
            ),
        )
        return _accepted()


def _accepted() -> Response:
    return JSONResponse({"accepted": True})


def _repo_name(payload: dict[str, Any]) -> str:
    return payload["repository"]["full_name"]
