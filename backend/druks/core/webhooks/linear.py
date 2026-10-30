from fastapi import HTTPException, status
from fastapi.responses import JSONResponse, Response

from druks.core import services
from druks.core.apis.linear import compute_delivery_key
from druks.services import ServiceNotConnectedError
from druks.signals import publish
from druks.webhooks import Webhook, verify_hmac_sha256


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

    def get_delivery_key(self) -> str:
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
        actor = self.data.get("actor") or {}
        await publish(
            "ticket.transitioned",
            payload={
                "source": "linear",
                "identifier": issue["identifier"],
                "status": state["name"],
                "title": issue["title"],
                "url": issue["url"],
                "project_name": project.get("name"),
                "labels": [label["name"] for label in issue["labels"]],
                "actor_id": actor.get("id"),
                "assignee_id": assignee.get("id"),
                "assignee_email": assignee.get("email"),
                "assignee_name": assignee.get("name"),
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
