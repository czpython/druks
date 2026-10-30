import hashlib
import hmac
import logging

from fastapi import HTTPException, status
from fastapi.responses import JSONResponse, Response

from druks.core import services
from druks.services import ServiceNotConnectedError
from druks.signals import publish
from druks.webhooks import Webhook

logger = logging.getLogger(__name__)


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

    def get_delivery_key(self) -> str:
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
                "actor_id": self.request.headers.get("x-jira-initiator"),
                "assignee_id": assignee.get("accountId"),
                "assignee_email": assignee.get("emailAddress"),
                "assignee_name": assignee.get("displayName"),
            },
        )
        return _accepted()
