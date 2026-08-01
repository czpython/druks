import hashlib
import hmac
import logging
from typing import Any

from fastapi import HTTPException, status
from fastapi.responses import JSONResponse, Response

from druks.contrib.ship.extension import Ship, secret_value
from druks.contrib.ship.ticketing.linear import compute_delivery_key
from druks.signals import publish
from druks.webhooks import Webhook, verify_hmac_sha256

logger = logging.getLogger(__name__)


class LinearEvents(Webhook):
    provider = "linear"
    category = "events"

    def request_is_authentic(self) -> bool:
        settings = Ship.settings()
        verify_hmac_sha256(
            self.raw_body,
            self.request.headers.get("linear-signature"),
            secret_value(settings.linear_webhook_secret),
            prefix="",
        )
        return True

    def delivery_key(self) -> str:
        headers = {key.lower(): value for key, value in self.request.headers.items()}
        return compute_delivery_key(headers, self.raw_body, self.data)

    def get_action(self) -> str:
        action = self.data.get("action")
        if self.data.get("type") == "Comment" and action == "create":
            return "comment_created"
        if action == "update" and "stateId" in (self.data.get("updatedFrom") or {}):
            return "state_transition"
        return "ticket_change"

    async def on_state_transition(self) -> Response:
        issue = self.data["data"]
        state = issue.get("state") or {}
        project = issue.get("project") or {}
        assignee = issue.get("assignee") or {}
        identifier = issue["identifier"]
        await publish(
            "ticket.transitioned",
            payload={
                "source": "linear",
                "identifier": identifier,
                "status": state.get("name", ""),
                "title": str(issue.get("title") or ""),
                "url": str(issue.get("url") or "") or None,
                "project_name": project.get("name"),
                "labels": [],
                "assignee_email": assignee.get("email") or None,
                "assignee_name": assignee.get("name") or None,
                "completed": state.get("type") == "completed",
                # State types are Linear's fixed vocabulary — terminal-ness can't
                # be read off status names, which every team customizes.
                "terminal": state.get("type") in ("completed", "canceled"),
            },
        )
        return _accepted()

    async def on_comment_created(self) -> Response:
        comment = self.data["data"]
        await publish(
            "ticket.commented",
            payload={
                "source": "linear",
                "parent_id": comment.get("parentId"),
                "issue_id": comment.get("issueId"),
            },
        )
        return _accepted()


def _accepted() -> Response:
    return JSONResponse({"accepted": True})


class JiraEvents(Webhook):
    provider = "jira"
    category = "events"

    def request_is_authentic(self) -> bool:
        settings = Ship.settings()
        webhook_secret = secret_value(settings.jira_webhook_secret)
        if not webhook_secret:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Jira webhook secret not configured.")
        provided = self.request.headers.get("x-druks-webhook-token") or ""
        if not hmac.compare_digest(provided, webhook_secret):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid Jira webhook token.")
        return True

    def get_action(self) -> str:
        return "issue_event"

    def delivery_key(self) -> str:
        # No delivery id from Automation: a retry resends the same body, so its
        # digest is the dedup key; a new transition changes the body.
        return hashlib.sha256(self.raw_body).hexdigest()[:16]

    @property
    def issue(self) -> dict[str, Any]:
        # Automation wraps the issue under ``issue``; a bare issue body works too.
        body = self.data if isinstance(self.data, dict) else {}
        nested = body.get("issue")
        return nested if isinstance(nested, dict) else body

    async def on_issue_event(self) -> Response:
        issue = self.issue
        fields = issue.get("fields") or {}
        key = str(issue.get("key") or "")
        if not key:
            self.log_ignored(event="jira", reason="jira_no_issue_key")
            return JSONResponse({"accepted": True})
        assignee = fields.get("assignee") or {}
        issue_status = fields.get("status") or {}
        await publish(
            "ticket.transitioned",
            payload={
                "source": "jira",
                "identifier": key,
                "status": issue_status.get("name") or "",
                "title": str(fields.get("summary") or ""),
                "url": self._issue_url(key),
                "project_name": (fields.get("project") or {}).get("name"),
                "labels": list(fields.get("labels") or []),
                "assignee_email": assignee.get("emailAddress") or None,
                "assignee_name": assignee.get("displayName") or None,
                "completed": False,
                # The "done" status category is Jira's terminal marker — it covers
                # Done/Closed/Won't Do however the workflow names its statuses.
                "terminal": (issue_status.get("statusCategory") or {}).get("key") == "done",
            },
        )
        return JSONResponse({"accepted": True})

    def _issue_url(self, key: str) -> str | None:
        settings = Ship.settings()
        base_url = settings.jira_base_url
        return f"{base_url.rstrip('/')}/browse/{key}" if base_url and key else None
