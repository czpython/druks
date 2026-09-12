import hashlib
import hmac
import logging

from fastapi import HTTPException, status
from fastapi.responses import JSONResponse, Response

from druks.core import services
from druks.core.apis.linear import compute_delivery_key
from druks.services import ServiceNotConnectedError
from druks.signals import publish
from druks.webhooks import Webhook, verify_hmac_sha256

logger = logging.getLogger(__name__)


class LinearEvents(Webhook):
    provider = "linear"
    category = "events"

    async def request_is_authentic(self) -> bool:
        try:
            row = await services.Linear.get()
        except ServiceNotConnectedError as error:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "Linear is not connected. Connect it in Settings → Connections → Services.",
            ) from error
        verify_hmac_sha256(
            self.raw_body,
            self.request.headers.get("linear-signature"),
            row.secrets["webhook_secret"],
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
        state = issue["state"]
        # An issue outside a project has no project. An unassigned issue has no assignee.
        project = issue.get("project") or {}
        assignee = issue.get("assignee") or {}
        await publish(
            "ticket.transitioned",
            payload={
                "source": "linear",
                "identifier": issue["identifier"],
                "status": state["name"],
                "title": issue["title"],
                "url": issue["url"],
                "project_name": project.get("name"),
                "labels": [],
                "assignee_id": assignee.get("id"),
                "assignee_email": assignee.get("email"),
                "assignee_name": assignee.get("name"),
                "completed": state["type"] == "completed",
                # Teams rename statuses, so the fixed Linear state type marks a terminal status.
                "terminal": state["type"] in ("completed", "canceled"),
            },
        )
        return _accepted()

    async def on_comment_created(self) -> Response:
        comment = self.data["data"]
        await publish(
            "ticket.commented",
            payload={
                "source": "linear",
                # Top-level comments have no parent.
                "parent_id": comment.get("parentId"),
                "issue_id": comment["issueId"],
            },
        )
        return _accepted()


def _accepted() -> Response:
    return JSONResponse({"accepted": True})


class JiraEvents(Webhook):
    provider = "jira"
    category = "events"

    async def request_is_authentic(self) -> bool:
        try:
            webhook_secret = (await services.Jira.get()).secrets["webhook_secret"]
        except ServiceNotConnectedError as error:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "Jira is not connected. Connect it in Settings → Connections → Services.",
            ) from error
        provided = self.request.headers.get("x-druks-webhook-token") or ""
        if not hmac.compare_digest(provided, webhook_secret):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid Jira webhook token.")
        return True

    def get_action(self) -> str:
        return "issue_event"

    def delivery_key(self) -> str:
        # Automation sends no delivery id. A retry sends the same body, so its digest is
        # the dedup key. A new transition changes the body.
        return hashlib.sha256(self.raw_body).hexdigest()[:16]

    async def on_issue_event(self) -> Response:
        # The Automation rule of the operator writes this body, not Jira. Druks accepts
        # one shape: the "Issue data" payload, with the REST issue JSON under ``issue``.
        # Any other body gets a 400, which the audit log of the rule shows.
        issue = self.data.get("issue")
        if not isinstance(issue, dict):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Body must carry the Jira issue JSON under 'issue'.",
            )
        fields = issue["fields"]
        issue_status = fields["status"]
        key = issue["key"]
        base_url = (await services.Jira.get()).identity["base_url"]
        # An unassigned issue has a null assignee. Privacy settings can hide the email.
        assignee = fields["assignee"] or {}
        await publish(
            "ticket.transitioned",
            payload={
                "source": "jira",
                "identifier": key,
                "status": issue_status["name"],
                "title": fields["summary"],
                "url": f"{base_url.rstrip('/')}/browse/{key}",
                "project_name": fields["project"]["name"],
                "labels": fields["labels"],
                "assignee_id": assignee.get("accountId"),
                "assignee_email": assignee.get("emailAddress"),
                "assignee_name": assignee.get("displayName"),
                "completed": False,
                # The "done" status category marks a terminal status, whatever its name.
                "terminal": issue_status["statusCategory"]["key"] == "done",
            },
        )
        return _accepted()
