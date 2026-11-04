from fastapi import HTTPException, status
from fastapi.responses import JSONResponse, Response

from druks.core import services
from druks.core.apis.linear import compute_delivery_key
from druks.services import ServiceNotConnectedError
from druks.signals import publish
from druks.webhooks import Webhook, verify_hmac_sha256

_CONTENT_FIELDS = frozenset({"title", "description", "labelIds", "projectId"})


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
        action, kind = self.data.get("action"), self.data.get("type")
        updated_from = self.data.get("updatedFrom") or {}
        if kind == "Comment" and action == "create":
            return "comment_created"
        if kind == "Issue" and action == "create":
            return "issue_created"
        if kind == "Issue" and action == "update" and "stateId" in updated_from:
            return "state_transition"
        # Linear sends an update for every change, a drag on the board included. Only a
        # change to what the issue says or where it belongs publishes ``ticket.updated``.
        if kind == "Issue" and action == "update" and _CONTENT_FIELDS & updated_from.keys():
            return "issue_updated"
        return "ticket_change"

    async def on_issue_created(self) -> Response:
        await publish("ticket.created", payload=self._ticket())
        return _accepted()

    async def on_issue_updated(self) -> Response:
        await publish("ticket.updated", payload=self._ticket())
        return _accepted()

    async def on_state_transition(self) -> Response:
        issue = self.data["data"]
        # An unassigned issue has no assignee.
        assignee = issue.get("assignee") or {}
        await publish(
            "ticket.transitioned",
            payload={
                **self._ticket(),
                "status": issue["state"]["name"],
                "assignee_id": assignee.get("id"),
                "assignee_email": assignee.get("email"),
                "assignee_name": assignee.get("name"),
            },
        )
        return _accepted()

    async def on_comment_created(self) -> Response:
        comment = self.data["data"]
        # A comment on a project or a document belongs to no ticket.
        if issue := comment.get("issue"):
            await publish(
                "ticket.commented",
                payload={
                    "source": "linear",
                    "ticket_id": issue["id"],
                    "identifier": issue["identifier"],
                    "title": issue["title"],
                    "url": issue["url"],
                    # A comment's issue carries neither its project nor its labels.
                    "project_name": None,
                    "labels": None,
                    "team": issue["teamId"],
                    "actor_id": self._actor_id(),
                    "comment_id": comment["id"],
                    "body": comment["body"],
                    # Top-level comments have no parent.
                    "parent_id": comment.get("parentId"),
                },
            )
        return _accepted()

    def _ticket(self) -> dict:
        """The keys every ``ticket.*`` event carries, for the issue in this delivery."""
        issue = self.data["data"]
        # An issue outside a project has no project.
        project = issue.get("project") or {}
        return {
            "source": "linear",
            # The identifier changes when the issue moves to another team; the id never does.
            "ticket_id": issue["id"],
            "identifier": issue["identifier"],
            "title": issue["title"],
            "url": issue["url"],
            "project_name": project.get("name"),
            "labels": [label["name"] for label in issue["labels"]],
            "team": issue["teamId"],
            "actor_id": self._actor_id(),
        }

    def _actor_id(self) -> str | None:
        return (self.data.get("actor") or {}).get("id")


def _accepted() -> Response:
    return JSONResponse({"accepted": True})
