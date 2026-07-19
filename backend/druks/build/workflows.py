import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast

from pydantic import BaseModel, Field

from druks.accounts.models import Account
from druks.build.contracts import (
    HumanFeedback,
    Implementation,
    ImplementationReview,
    PlanData,
    PlanReview,
)
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
from druks.ticketing.enums import SemanticStatus
from druks.workflows import FatalError, Gate, Workflow, step

from .extension import Build
from .policy import RepoPolicy
from .workspace import RepoWorkspace

if TYPE_CHECKING:
    from druks.sandbox.host import Sandbox

logger = logging.getLogger(__name__)


# The github MCP server build ships into its own runs — build's requirement
# (there is no build without github), not an operator-facing catalog entry.
# Its token is per-repo, minted from the reviewer app at workspace setup.
GITHUB_MCP_NAME = "github"
GITHUB_MCP_URL = "https://api.githubcopilot.com/mcp/"


# Build's one external gate: code review happens on the PR. `action` spans the webhook's
# review vocab (approve, request_changes — the only actions a resumer can send) plus the
# operator's UI decisions (revise_contract, cancel). on_wait un-drafts the PR and pings
# the assignee.
class ReviewWork(Gate):
    action: Literal["approve", "request_changes", "revise_contract", "cancel"]
    reviewer: str | None = None
    body: str | None = None

    @classmethod
    async def on_wait(cls, workflow: Workflow) -> None:
        build = cast("BuildWorkflow", workflow)
        await build._set_pr_draft(draft=False)
        await build._request_assignee_review()


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

    def __init__(self) -> None:
        super().__init__()
        # The run's working memory. Durable by determinism: a recovery re-runs
        # run() from the top with every agent call and gate reply memoized, so
        # these rebuild to exactly what the live pass held.
        self._plans: list[PlanData] = []
        self._plan_reviews: list[PlanReview] = []
        # Where _plan_reviews stood at the latest plan draft, so
        # reviewer_requirements only spans reviews of the current plan.
        self._reviews_at_plan = 0
        self._implementation_reviews: list[ImplementationReview] = []
        self._implementation_results: list[Implementation] = []
        self._human_feedback: list[HumanFeedback] = []
        # The questions the operator answered ({question, answer}), fed to the next plan
        # pass; empty on the first pass and after a plain re-plan.
        self._answered: list[dict[str, str]] = []
        # The operator's free-text note from the latest review reply, quoted to the
        # next plan pass; empty when they left none.
        self._note = ""

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
        start_result = await cls.start(
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
        item.update(build_run_id=start_result.run_id)
        # Back onto the active board: a scoped item re-enters flight when its
        # build starts. History is for items at rest in a handoff lane.
        item.set_status(None)
        return start_result.run_id

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
        work_item_id = self.subject["id"] if self.subject else None
        self._work_item_id = work_item_id
        # Resolve the repo's policy + profile and the operator settings inside
        # steps so their reads are memoized — the body itself does no IO, and
        # replay reuses the values.
        snapshot = await self._load_policy_and_profile()
        self._policy = RepoPolicy.model_validate(snapshot["policy"])
        self._profile = snapshot["profile"]
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
        return {
            "verification": await self._policy.verification_block(
                profile=self._profile, repo=self.input.repo
            ),
            **await super().get_prompt_context(**context),
        }

    @step
    async def _load_policy_and_profile(self) -> dict[str, Any]:
        # One memoized read: the work item's dispatch-time snapshot when present,
        # else the live policy + the repo's profiled facts.
        item = WorkItem.get(self._work_item_id) if self._work_item_id else None
        if item and item.extension_config_snapshot:
            return item.extension_config_snapshot
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
        """Plan → questions? park for the operator's answers and re-plan : approve.
        True → implement; cancel raises."""
        answered: list[dict[str, str]] = []
        note = ""
        while True:
            plan = await self.generate_plan(answered, note)
            # No open questions and a clean grade under an auto-approve policy ships
            # the plan without asking the operator.
            if not plan.questions:
                grade = await self.review_plan()
                if grade.decision == ReviewDecision.APPROVE and (
                    self._policy.plan_approval_gate(self._settings.auto_dispatch_on_plan_approval)
                    == "none"
                ):
                    return True
            reply = await self.review(questions=plan.questions)
            if reply["action"] == "cancel":
                raise FatalError("cancelled at plan review")
            if reply["action"] == "approve" and not plan.questions:
                return True
            answered = plan.get_answered(reply["answers"])
            note = reply["note"]

    async def _implement_phase(self) -> None:
        while True:
            await self.implement()
            evaluation = await self.evaluate()
            if evaluation.verdict == EvaluationVerdict.PASS:
                if self._settings.review_code:
                    await Build.review_code()
                if await self._work_gate():
                    return
                continue
            if evaluation.verdict == EvaluationVerdict.FAIL and (
                len(self._implementation_results) < self._settings.max_implementation_revisions
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
            return await self._triage(decision)
        if decision.action == "revise_contract":
            await self.revise_contract()
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

    async def _triage(self, decision: ReviewWork) -> bool:
        feedback = await self.triage_feedback(decision)
        self._human_feedback.append(feedback)
        if feedback.triage_action == HumanFeedbackAction.CHANGE_REQUIRED:
            return False  # loop → implement
        if feedback.triage_action == HumanFeedbackAction.CONTRACT_CHANGE_REQUIRED:
            await self.revise_contract()
            return False
        if feedback.triage_action == HumanFeedbackAction.CLOSE:
            raise FatalError("closed at human triage")
        # NO_CHANGE / QUESTION → re-park
        return await self._work_gate()

    # The phase methods are plain workflow-body code: the agent calls inside them
    # memoize themselves, so on a recovery the pure parts re-run deterministically
    # and rebuild the in-memory diary (set_state included — it is body-only). Only
    # side-effecting IO beyond an agent call (GitHub writes, child starts, event
    # records) keeps its own @step.
    async def generate_plan(
        self, answered: list[dict[str, str]] | None = None, note: str = ""
    ) -> PlanData:
        # The operator's guidance reaches the agent via answered_questions and
        # operator_note — not on the plan.
        self._answered = answered or []
        self._note = note
        return self._add_plan(await Build.generate_plan())

    async def review_plan(self) -> PlanReview:
        grade = await Build.review_plan()
        self._plan_reviews.append(grade)
        return grade

    async def revise_contract(self) -> PlanData:
        return self._add_plan(await Build.revise_contract())

    async def implement(self) -> Implementation:
        delivery = await Build.implement()
        if not self.pr_number:
            # First delivery: the implementer provisioned the branch + draft PR alongside
            # its commits; publish the pair (the run.state signal mirrors it onto the item).
            await self.set_state(branch=delivery.branch, pr_number=delivery.pr_number)
        self._implementation_results.append(delivery)
        return delivery

    async def evaluate(self) -> ImplementationReview:
        review = await Build.evaluate_implementation()
        self._implementation_reviews.append(review)
        return review

    async def triage_feedback(self, decision: ReviewWork) -> HumanFeedback:
        parsed = await Build.triage_human_feedback()
        return HumanFeedback(
            reviewer=decision.reviewer or "(triage)",
            body=parsed.body,
            triage_action=parsed.action,
            triage_body=parsed.body,
            question=parsed.question,
            implementation_instructions=parsed.implementation_instructions,
        )

    @step
    async def merge(self) -> None:
        github = get_github_client(load_settings())
        pull_request = await github.get_pull_request(self.input.repo, self.pr_number)
        if pull_request.get("state") == "closed":
            return
        await github.squash_merge_pull_request(self.input.repo, self.pr_number)
        if self._policy.delete_branch:
            await _delete_branch(self.input.repo, self.branch)

    def _add_plan(self, plan: PlanData) -> PlanData:
        self._plans.append(plan)
        self._reviews_at_plan = len(self._plan_reviews)
        return plan

    # The provisioned branch + PR, published by the first implement — None until then
    # (planning runs against the default branch, and there is no PR to point at).
    @property
    def branch(self) -> str | None:
        return getattr(self.state, "branch", None)

    @property
    def pr_number(self) -> int | None:
        return getattr(self.state, "pr_number", None)

    @property
    def plan_drafts(self) -> list[PlanData]:
        return list(self._plans)

    @property
    def plan(self) -> PlanData:
        return self._plans[-1] if self._plans else PlanData()

    @property
    def current_plan(self) -> str:
        return self.plan.plan_markdown

    @property
    def acceptance_criteria(self):
        return self.plan.acceptance_criteria

    @property
    def questions(self):
        return self.plan.questions

    @property
    def plan_reviews(self) -> list[PlanReview]:
        return list(self._plan_reviews)

    @property
    def implementation_reviews(self) -> list[ImplementationReview]:
        return list(self._implementation_reviews)

    @property
    def assignee_github_login(self) -> str | None:
        for review in reversed(self.plan_reviews):
            if review.assignee_github_login:
                return review.assignee_github_login
        return None

    @property
    def plan_revision(self) -> int:
        return len(self._plans)

    @property
    def reviewer_requirements(self) -> list[PlanReview]:
        # Approve-with-required-changes verdicts on the current plan draft
        # only — reviews of superseded drafts don't bind the implementer.
        return [
            review
            for review in self._plan_reviews[self._reviews_at_plan :]
            if review.decision == ReviewDecision.APPROVE_WITH_REQUIRED_CHANGES
            and review.body.strip()
        ]

    @property
    def implementation_revision(self) -> int:
        # Prior finalized implement attempts — 0 on the first. Read by the prompt header.
        return len(self._implementation_results)

    @property
    def human_feedback(self) -> list[HumanFeedback]:
        return list(self._human_feedback)

    @property
    def _last_implement(self) -> Implementation | None:
        return self._implementation_results[-1] if self._implementation_results else None

    @property
    def finalized_base_sha(self) -> str | None:
        return self._last_implement.base_sha if self._last_implement else None

    @property
    def finalized_pr_sha(self) -> str | None:
        # What the harness pushed to origin/<pr> — the finalize lease check, so a
        # concurrent write to the PR branch is rejected, not overwritten.
        return self._last_implement.head_sha if self._last_implement else None

    @property
    def related_repos(self) -> list[ProjectRepo]:
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

    @property
    def answered_questions(self) -> list[dict[str, str]]:
        return self._answered

    @property
    def operator_note(self) -> str:
        return self._note

    @step
    async def _clear_draft(self) -> None:
        await self._set_pr_draft(draft=False)

    @step
    async def _push_ticket_status(self, status: SemanticStatus) -> None:
        work_item = WorkItem.get_for_pr(
            repo=self.input.repo,
            pr_number=self.pr_number,
        )
        if work_item:
            await work_item.set_remote_status(status)

    async def _request_assignee_review(self) -> None:
        login = self.assignee_github_login
        if not login or not self.input.repo or not self.pr_number:
            return
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

    async def _set_pr_draft(self, *, draft: bool) -> None:
        if not self.input.repo or not self.pr_number:
            return
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
        self._repo = project_repo.full_name

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
        repo = self._repo
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
            "repo": self._repo,
            "skills_catalog": [
                {"name": skill.name, "description": skill.description}
                for skill in Skill.list_enabled()
            ],
            **await super().get_prompt_context(**context),
        }


async def _delete_branch(repo: str, branch: str | None) -> None:
    if not branch:
        return
    try:
        await get_github_client(load_settings()).delete_branch(repo, branch)
    except Exception:  # noqa: BLE001 — cleanup only
        logger.warning("Could not delete branch %s on %s.", branch, repo, exc_info=True)
