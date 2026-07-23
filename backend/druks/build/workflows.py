import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from druks.accounts.models import Account
from druks.build.contracts import ImplementationOutput, ReviewWork, ScopeBriefOutput
from druks.build.enums import (
    EvaluationVerdict,
    HumanFeedbackAction,
    ReviewDecision,
)
from druks.build.models import Project, ProjectRepo, WorkItem
from druks.core.apis.github import get_github_client, get_reviewer_github_client
from druks.sandbox import repo as _repo
from druks.sandbox.datastructures import RequiredMcpServer
from druks.sandbox.layout import (
    get_github_token_remote_path,
    get_related_root,
    get_repo_root,
    get_work_root,
)
from druks.settings import load_settings
from druks.skills.models import Skill
from druks.ticketing.datastructures import Ticket
from druks.ticketing.enums import SemanticStatus
from druks.workflows import FatalError, Gate, Run, Workflow, step

from .extension import Build
from .journal import BuildJournal
from .policy import RepoPolicy
from .prompt_context import BuildPromptContext
from .workspace import RepoWorkspace

if TYPE_CHECKING:
    from druks.sandbox.host import Sandbox

logger = logging.getLogger(__name__)


# The github MCP server build ships into its own runs — build's requirement
# (there is no build without github), not an operator-facing catalog entry.
# Its token is per-repo, minted from the reviewer app at workspace setup.
GITHUB_MCP_NAME = "github"
GITHUB_MCP_URL = "https://api.githubcopilot.com/mcp/"


@dataclass(frozen=True, kw_only=True)
class BuildWorkspace(RepoWorkspace):
    # The base RepoWorkspace brings the cloned repo + token; a build run adds the
    # PR branch and the reviewer MCP token its agents push and review through.
    branch: str | None = None
    # Reviewer-extension installation token for build's github MCP server,
    # minted per repo from the reviewer GitHub App. Required — there is no
    # build without github.
    mcp_token: str

    @property
    def workspace_root(self) -> str:
        return get_work_root(self.sandbox.ssh_username)

    def get_required_mcp_servers(self) -> tuple[RequiredMcpServer, ...]:
        return (RequiredMcpServer(name=GITHUB_MCP_NAME, url=GITHUB_MCP_URL, token=self.mcp_token),)

    def get_agent_run_kwargs(self, **kwargs: Any) -> dict[str, Any]:
        # Agents clone related repos on demand under get_related_root; grant file-tool
        # access to the whole dir (Claude scopes file access to cwd + add_dirs;
        # Codex has full FS access and ignores it). get_related_root is never the repo
        # cwd — Claude wedges (no stdout, forever) on ``--add-dir <cwd>``.
        kwargs = super().get_agent_run_kwargs(**kwargs)
        kwargs["add_dirs"] = (get_related_root(self.sandbox.ssh_username),)
        return kwargs


class BuildWorkflow(Workflow):
    steps_reuse_sandbox = True
    workspace_class = BuildWorkspace
    journal_class = BuildJournal
    journal: BuildJournal

    class Settings(BaseModel):
        auto_dispatch_on_plan_approval: bool = Field(
            default=False,
            title="Auto-dispatch on plan approval",
            description="Skip the human gate when the plan reviewer approves cleanly.",
        )
        max_implementation_revisions: int = Field(
            default=5,
            ge=1,
            le=20,
            title="Max implementation revisions",
            description="Implement/review round-trips before parking for a human.",
        )
        review_code: bool = Field(
            default=True,
            title="Review code",
            description="Run the line-level reviewer after a passing evaluation.",
        )

    @classmethod
    async def dispatch(
        cls,
        *,
        work_item_id: int,
        assignee_email: str | None = None,
        assignee_name: str | None = None,
    ) -> str:
        item = WorkItem.get(work_item_id)
        if not item:
            raise ValueError(f"dispatching a build for unknown work item {work_item_id}")
        # The assignee's account runs the calls; the owner fields stay input.
        assignee = Account.get_for_username(assignee_email.strip()) if assignee_email else None
        run_id = await cls.start(
            subject=WorkItem.subject_for(item.id),
            account_id=assignee.id if assignee else None,
            repo=item.repo,
            source=item.source,
            ticket_ref=item.remote_key,
            remote_title=item.title,
            remote_url=item.remote_url,
            task_owner_email=assignee_email,
            task_owner_name=assignee_name,
        )
        # start() hands back the live run's id on a duplicate dispatch; only a
        # genuinely new run is a fresh attempt. Point the item at it and drop the
        # prior attempt's branch/PR so a late close for the old PR can't resolve
        # this item onto the new run and cancel it — a duplicate keeps the live
        # run's routing untouched.
        if item.build_run_id != run_id:
            item.update(build_run_id=run_id, branch=None, pr_number=None)
        # Back onto the active board: a scoped item re-enters flight when its
        # build starts. History is for items at rest in a handoff lane.
        item.set_status(None)
        return run_id

    async def run_multistep(
        self,
        repo: str,
        issue_number: int | None = None,
        source: str = "github",
        # Human-readable ticket reference ("ACME-270", "#42"). Same value the
        # WorkItem keeps as ``remote_key``; carried separately on the input
        # so the workflow can render it into prompts / PR titles without a
        # WorkItem fetch.
        ticket_ref: str | None = None,
        # Ticket title as intake received it; the implementer uses it for the PR title.
        remote_title: str | None = None,
        remote_url: str | None = None,
        task_owner_email: str | None = None,
        task_owner_name: str | None = None,
    ) -> None:
        # Resolve the repo's policy + profile and the operator settings inside
        # steps so their reads are memoized — the body itself does no IO, and
        # replay reuses the values.
        resolved = await self._load_policy_and_profile()
        self._policy = RepoPolicy.model_validate(resolved["policy"])
        self._profile = resolved["profile"]
        self._settings = await self._load_settings()

        if not await self._plan_phase():
            return
        await self._implement_phase()

    async def get_workspace_kwargs(self, sandbox: "Sandbox") -> dict[str, Any]:
        # The BuildWorkspace fields: mint a fresh GitHub token, push it, and clone the
        # primary repo (at branch) into the VM. Re-runs per agent call — the clone is
        # idempotent (one test -d on a warm VM) so it's cheap, and the ~60min token
        # mints fresh each time. Warm-host rotation depends on this per-call rebuild:
        # never hoist the clone to a once-per-run step, or a rotated-in bare VM would
        # have no working tree. Related repos are NOT pre-cloned: agents clone the
        # ones they actually need under get_related_root (the prompt names them, the
        # credential helper handles auth). The mkdir keeps Claude's --add-dir target
        # valid before the first on-demand clone.
        repo = self.input.repo
        # Planning agents run before the first implement provisions the branch — their
        # VMs clone the default branch; every agent after delivery gets the PR branch.
        branch = self.branch
        github_token = await get_github_client(load_settings()).token_for_repo(repo)
        await sandbox.write_secret(
            secret=github_token, remote=get_github_token_remote_path(sandbox.ssh_username)
        )
        await _repo.ensure(
            sandbox,
            repo_url=f"https://github.com/{repo}",
            ref=branch,
            target_path=get_repo_root(sandbox.ssh_username),
        )
        await sandbox.exec(["mkdir", "-p", get_related_root(sandbox.ssh_username)], timeout=10.0)
        try:
            mcp_token = await get_reviewer_github_client(load_settings()).token_for_repo(repo)
        except Exception as error:
            # There is no build without github: agents push and review through
            # the github MCP, so a run that can't mint its token fails here,
            # loudly, instead of degrading mid-run.
            raise FatalError(
                f"Could not mint the reviewer GitHub App token for {repo}; build "
                "requires it for its github MCP server. Configure "
                "GITHUB_REVIEWER_APP_ID and its private key."
            ) from error
        return {
            **await super().get_workspace_kwargs(sandbox),
            "repo": repo,
            "branch": branch,
            "github_token": github_token,
            "mcp_token": mcp_token,
        }

    async def get_prompt_context(self, **context: Any) -> dict[str, Any]:
        prompt_context = BuildPromptContext(
            repo=self.input.repo,
            branch=self.branch,
            pr_number=self.pr_number,
            ticket_ref=self.input.ticket_ref,
            source=self.input.source,
            issue_number=self.input.issue_number,
            task_owner_name=self.input.task_owner_name,
            task_owner_email=self.input.task_owner_email,
            related_repos=self._related_repos(),
            journal=self.journal,
        )
        return {
            "verification": await self._policy.verification_block(
                profile=self._profile, repo=self.input.repo
            ),
            "build": prompt_context,
            **await super().get_prompt_context(**context),
        }

    @step
    async def _load_policy_and_profile(self) -> dict[str, Any]:
        # One memoized read: the live policy + the repo's profiled facts.
        policy = await RepoPolicy.resolve(self.input.repo)
        # A build dispatches against a work item whose repo is registered.
        target = ProjectRepo.get_for_repo(self.input.repo)
        return {
            "policy": policy.model_dump(mode="json"),
            "profile": target.effective_profile(),
        }

    @step
    async def _load_settings(self) -> "BuildWorkflow.Settings":
        # A step so replay reuses the values the run started with, not later edits.
        return self.settings()

    async def _plan_phase(self) -> bool:
        """Gate mode: plan → park. Auto mode: machine review with one bounded
        redraft. True → implement; cancel raises."""
        gate = self._policy.plan_approval_gate(self._settings.auto_dispatch_on_plan_approval)
        answered: list[dict[str, str]] = []
        note = ""
        while True:
            reviewer_notes = ""
            redrafted = False
            critique = ""
            while True:
                plan = await Build.generate_plan(
                    answered_questions=answered, operator_note=note, reviewer_notes=reviewer_notes
                )
                if gate != "none" or plan.questions:
                    break
                grade = await Build.review_plan()
                if grade.decision == ReviewDecision.APPROVE:
                    return True
                if redrafted:
                    # Exhausted — park below, critique on the ask.
                    critique = grade.body
                    break
                redrafted = True
                reviewer_notes = grade.body
            reply = await self.review(questions=plan.questions, context=critique)
            if reply.action == "cancel":
                raise FatalError("cancelled at plan review")
            if reply.action == "approve" and not plan.questions:
                return True
            answered = plan.get_answered(reply.answers)
            note = reply.note

    async def _implement_phase(self) -> None:
        while True:
            await self.implement()
            evaluation = await Build.evaluate_implementation()
            if evaluation.verdict == EvaluationVerdict.PASS:
                if self._settings.review_code:
                    await Build.review_code()
                if await self._work_gate():
                    return
                continue
            if evaluation.verdict == EvaluationVerdict.FAIL and (
                self.journal.implementation_revision < self._settings.max_implementation_revisions
            ):
                continue
            if await self._work_gate():
                return
            continue

    async def _work_gate(self) -> bool:
        """Park for work approval. True → terminal (merged or review-finished);
        False → rework looped (triage routed to implement/revise)."""
        if self._policy.implementation_approval_gate() == "none":
            return await self._approved_work()
        decision = await ReviewWork.wait(
            input_request={"presentation": "external", "label": "Review implementation"}
        )
        if decision.action == "approve":
            return await self._approved_work()
        if decision.action == "request_changes":
            return await self._triage()
        if decision.action == "revise_contract":
            await Build.revise_contract()
            return False
        if decision.action == "cancel":
            await self._push_ticket_status(SemanticStatus.CANCELED)
            raise FatalError("cancelled at work review")
        return False

    async def _approved_work(self) -> bool:
        # GitHub announces the merge; the pr.closed reaction settles shipped.
        if self._policy.on_approval == "merge":
            await self.merge()
        else:
            await self._clear_draft()
        return True

    async def _triage(self) -> bool:
        feedback = await Build.triage_human_feedback()
        if feedback.action == HumanFeedbackAction.CHANGE_REQUIRED:
            return False  # loop → implement
        if feedback.action == HumanFeedbackAction.CONTRACT_CHANGE_REQUIRED:
            await Build.revise_contract()
            return False
        if feedback.action == HumanFeedbackAction.CLOSE:
            raise FatalError("closed at human triage")
        # NO_CHANGE / QUESTION → re-park
        return await self._work_gate()

    # Body code, never @step: the agent calls inside memoize themselves and land
    # on the record, and set_state must re-run on replay — a @step would skip them.
    async def implement(self) -> ImplementationOutput:
        delivery = await Build.implement()
        # A bail is a stop, not a result: the implementer hit a contradiction in the
        # binding requirements and couldn't deliver. Fail the run with its own reason,
        # read off the dashboard instead of dug out of the transcript.
        if delivery.status == "needs_clarification":
            raise FatalError(f"implementation needs clarification: {delivery.summary}")
        if not self.pr_number:
            # First delivery: the implementer provisioned the branch + draft PR alongside
            # its commits; publish the pair (the run.state signal mirrors it onto the item).
            await self.set_state(branch=delivery.branch, pr_number=delivery.pr_number)
        return delivery

    @step
    async def merge(self) -> None:
        github = get_github_client(load_settings())
        pull_request = await github.get_pull_request(self.input.repo, self.pr_number)
        if pull_request.get("state") == "closed":
            return
        await github.squash_merge_pull_request(self.input.repo, self.pr_number)
        if self._policy.delete_branch:
            try:
                await github.delete_branch(self.input.repo, self.branch)
            except Exception:  # noqa: BLE001 — cleanup only
                logger.warning(
                    "Could not delete branch %s on %s.",
                    self.branch,
                    self.input.repo,
                    exc_info=True,
                )

    # The provisioned branch + PR, published by the first implement — None until then
    # (planning runs against the default branch, and there is no PR to point at).
    @property
    def branch(self) -> str | None:
        return getattr(self.state, "branch", None)

    @property
    def pr_number(self) -> int | None:
        return getattr(self.state, "pr_number", None)

    def _related_repos(self) -> list[ProjectRepo]:
        # The project's sibling repos (the prompt reads full_name + purpose).
        target = (self.input.repo or "").strip().lower()
        project = Project.get_for_repo(self.input.repo) if self.input.repo else None
        if not project:
            return []
        return [
            repo
            for repo in project.repos
            if (repo.full_name or "").strip() and repo.full_name.lower() != target
        ]

    @step
    async def _clear_draft(self) -> None:
        await self.set_pr_draft(draft=False)

    @step
    async def _push_ticket_status(self, status: SemanticStatus) -> None:
        work_item = WorkItem.get_for_pr(
            repo=self.input.repo,
            pr_number=self.pr_number,
        )
        if work_item:
            await work_item.set_remote_status(status)

    async def request_assignee_review(self) -> None:
        login = self.journal.assignee_github_login
        if login and self.input.repo and self.pr_number:
            try:
                await get_github_client(load_settings()).request_pull_request_reviewers(
                    self.input.repo, self.pr_number, [login]
                )
            except Exception:  # noqa: BLE001 — a missed ping must not fail the park
                logger.warning(
                    "could not request review from %s on %s#%s",
                    login,
                    self.input.repo,
                    self.pr_number,
                )

    async def set_pr_draft(self, *, draft: bool) -> None:
        if self.input.repo and self.pr_number:
            try:
                await get_github_client(load_settings()).set_pull_request_draft_state(
                    self.input.repo, self.pr_number, draft=draft
                )
            except Exception:  # noqa: BLE001 — a draft merge fails loudly anyway
                logger.warning(
                    "Could not set draft=%s on %s#%s.", draft, self.input.repo, self.pr_number
                )


class Profile(Workflow):
    """Profiles a repo once, when it joins a project: the repo_profiler agent
    reads the checkout and reports stack, verification commands, and recommended
    skills onto ProjectRepo.profile. ``refresh_only`` skips the agent — it
    re-applies the operator's pinned verification over the stored baseline, for
    the reaction to a .druks/build/config.yml push."""

    workspace_class = RepoWorkspace

    async def run(self, repo_id: int, refresh_only: bool = False) -> None:
        # Every dispatch site verifies the repo exists first; a build never
        # profiles a repo that isn't there.
        project_repo = ProjectRepo.get(repo_id)

        if refresh_only:
            baseline = project_repo.profile.get("baseline") or {}
        else:
            baseline = await Build.repo_profiler(repo=project_repo.full_name)
            # The agent picks from the catalog it was handed, but a skill can be
            # disabled between prompt render and result — read the ground truth again.
            enabled = {skill.name for skill in Skill.list_enabled()}
            baseline["recommended_skills"] = [
                name for name in baseline["recommended_skills"] if name in enabled
            ]

        policy = await RepoPolicy.resolve(project_repo.full_name)
        effective = dict(baseline)
        if policy.verification:
            effective["verification"] = policy.verification.model_dump(mode="json")
        project_repo.set_profile(baseline=baseline, effective=effective)

    async def get_workspace_kwargs(self, sandbox: "Sandbox") -> dict[str, Any]:
        repo = ProjectRepo.get(self.input.repo_id).full_name
        github_token = await get_github_client(load_settings()).token_for_repo(repo)
        await sandbox.write_secret(
            secret=github_token, remote=get_github_token_remote_path(sandbox.ssh_username)
        )
        await _repo.ensure(
            sandbox,
            repo_url=f"https://github.com/{repo}",
            ref=None,
            target_path=get_repo_root(sandbox.ssh_username),
        )
        return {
            **await super().get_workspace_kwargs(sandbox),
            "repo": repo,
            "github_token": github_token,
        }

    async def get_prompt_context(self, **context: Any) -> dict[str, Any]:
        return {
            "repo": ProjectRepo.get(self.input.repo_id).full_name,
            "skills_catalog": [
                {"name": skill.name, "description": skill.description}
                for skill in Skill.list_enabled()
            ],
            **await super().get_prompt_context(**context),
        }


# What the gate asks the operator while the run is parked, by brief status. Scope is
# answered on the ticket (external), so the ask is just the dashboard's one-liner.
_PARKED_ASK = {
    "needs_answers": {"presentation": "external", "label": "Answer scope questions"},
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
        item = WorkItem.get_for_remote_key(source=ticket.provider, remote_key=ticket.key)
        if not item:
            target = ProjectRepo.lookup(project_name=ticket.project_name, labels=ticket.labels)
            project = Project.get_for_repo(target.full_name) if target else None
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
        assignee = None
        if ticket.assignee_email:
            assignee = Account.get_for_username(ticket.assignee_email.strip())
        return await cls.start(
            subject=WorkItem.subject_for(item.id),
            account_id=assignee.id if assignee else None,
            remote_key=ticket.key,
            source=ticket.provider,
        )

    @classmethod
    def parked_for(cls, work_item_id: int) -> Run | None:
        runs = Run.list_for_subject("work_item", str(work_item_id), kind=cls.kind)
        return next((run for run in runs if run.is_parked), None)

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
        target = ProjectRepo.get_for_repo(item.repo)
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
