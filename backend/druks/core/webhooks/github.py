from datetime import UTC, datetime
from typing import Any, ClassVar

from fastapi import HTTPException, status
from fastapi.responses import JSONResponse, Response

from druks.core.apis.github import GITHUB
from druks.services.exceptions import ServiceNotConnectedError
from druks.services.models import ServiceIdentity
from druks.signals import publish
from druks.webhooks import Webhook, verify_hmac_sha256

_REVIEW_ACTION = {"APPROVED": "approve", "CHANGES_REQUESTED": "request_changes"}

# GitHub's standing for the commenter on that repo. These three write to it;
# everyone else is a passer-by, and on a public repo that is the whole internet.
_WRITERS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})


class GitHubEvents(Webhook):
    """Verifies the GitHub HMAC, then emits ``pr.review_submitted`` /
    ``pr.closed`` — normalized facts, no WorkItem knowledge."""

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
            identity = await ServiceIdentity.get(GITHUB)
        except ServiceNotConnectedError as error:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "GitHub is not connected — connect it in Settings → Services.",
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
        if sender["type"] != "User":
            return _accepted()
        review, pull_request = self.data["review"], self.data["pull_request"]
        action = _REVIEW_ACTION.get(review["state"].upper())
        if not action:
            return _accepted()
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
        # GitHub files pull-request comments under issues; only those carry
        # ``pull_request``. A non-User sender is an app talking to itself.
        issue, sender = self.data["issue"], self.data["sender"]
        if sender["type"] != "User" or "pull_request" not in issue:
            return _accepted()
        await publish(
            "pr.commented",
            repo=_repo_name(self.data),
            pr_number=issue["number"],
            payload={
                "author": sender["login"],
                "author_can_write": self.data["comment"]["author_association"] in _WRITERS,
                "body": self.data["comment"]["body"],
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
