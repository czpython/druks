import logging

from druks.accounts.models import Account
from druks.build.extension import Build
from druks.build.models import Project, ProjectRepo, WorkItem
from druks.build.scoping.contracts import ScopeBriefOutput
from druks.ticketing.datastructures import Ticket
from druks.workflows import Gate, Run, RunState, Workflow

logger = logging.getLogger(__name__)

# What the gate asks the operator while the run is parked, by brief status. Scope is
# answered on the ticket (external), so the ask is just the dashboard's one-liner.
_PARKED_ASK = {
    "needs_answers": {"presentation": "external", "label": "Answer scope questions"},
    "split_recommended": {"presentation": "external", "label": "Decide on proposed split"},
}


class ScopeReply(Gate):
    """No fields: the operator answers by commenting on the ticket — the agent
    re-reads the thread on resume — so the reply only needs to wake the run."""


class Scope(Workflow):
    @classmethod
    async def dispatch(cls, *, ticket: Ticket) -> str | None:
        # The scoped label is the done marker — remove it to force a re-scope.
        if ticket.has_label(Build.settings().scoper_scoped_label):
            return None
        item = WorkItem.get_by_remote_key(source=ticket.provider, remote_key=ticket.key)
        if not item:
            target = ProjectRepo.get_by_ticket_signals(
                project_name=ticket.project_name, labels=ticket.labels
            )
            project = Project.get_by_repo(target.full_name) if target else None
            if not project:
                logger.info("No project routes %s; not scoping.", ticket.key)
                return None
            item = WorkItem.create(
                project_id=project.id,
                source=ticket.provider,
                title=ticket.title or ticket.key,
                remote_key=ticket.key,
                remote_url=ticket.url,
                repo=target.full_name,
            )
        account_id, stripped_assignee = Account.resolve_assignee(ticket.assignee_email)
        return await cls.start(
            subject=WorkItem.subject_for(item.id),
            account_id=account_id,
            assignee_email=stripped_assignee,
            remote_key=ticket.key,
            source=ticket.provider,
        )

    @classmethod
    def parked_for(cls, work_item_id: int) -> Run | None:
        runs = Run.list_for_subject("work_item", str(work_item_id), kind=cls.kind)
        return next((run for run in runs if run.state == RunState.PENDING_INPUT.value), None)

    async def get_prompt_context(self, **context: object) -> dict[str, object]:
        # Everything the agent needs beyond the ticket it fetches itself: where
        # the work lands (the subject's repo + siblings), the marks it must leave
        # on the tracker, and the target repo's recommended skills for the brief's
        # Skills section.
        item = WorkItem.get(self.subject["id"])
        siblings = [
            {"full_name": r.full_name, "purpose": r.purpose or ""}
            for r in item.project.repos
            if r.full_name != item.repo
        ]
        # The ticket routed through this repo to exist, so it's registered.
        target = ProjectRepo.get_by_full_name(item.repo)
        settings = Build.settings()
        return {
            "target_repo": item.repo,
            "target_purpose": target.purpose or "",
            "repos": siblings,
            "scoped_label": settings.scoper_scoped_label,
            "post_refinement_status": Build.post_refinement_status(item.source),
            "recommended_skills": target.effective_profile().get("recommended_skills", []),
            **await super().get_prompt_context(**context),
        }

    async def run_multistep(self, remote_key: str, source: str = "linear") -> ScopeBriefOutput:
        while True:
            brief = await Build.scope(remote_key=remote_key, source=source)
            if brief.status == "ready":
                return brief
            await ScopeReply.wait(input_request=_PARKED_ASK[brief.status])
