import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from druks.accounts.models import Account
from druks.contrib.software_factory.contracts import ImplementationOutput, ReviewWork
from druks.contrib.software_factory.enums import (
    EvaluationVerdict,
    HumanFeedbackAction,
    Resolution,
    ReviewDecision,
)
from druks.contrib.software_factory.models import ProjectRepo, WorkItem
from druks.contrib.software_factory.ticketing.enums import TicketStatus
from druks.core.apis.github import get_github_client
from druks.core.services import Github
from druks.mcp.inbound import get_druks_mcp_server
from druks.sandbox.datastructures import RequiredMcpServer
from druks.sandbox.layout import get_related_root, get_work_root
from druks.sandbox.models import SecretRef
from druks.services.exceptions import ServiceNotConnectedError
from druks.settings import load_settings
from druks.skills.models import Skill
from druks.workflows import FatalError, Workflow, step
from druks.workspaces import RepoWorkspace

from .app import SoftwareFactory
from .constants import GITHUB_MCP_NAME, GITHUB_MCP_URL, TICKET_TOOLS
from .datastructures import PullRequest
from .github import get_review_actor
from .journal import BuildJournal
from .policy import PlanGate, RepoPolicy
from .prompt_context import BuildPromptContext

if TYPE_CHECKING:
    from druks.sandbox.host import Host

logger = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class BuildWorkspace(RepoWorkspace):
    skills: tuple[str, ...]

    @property
    def workspace_root(self) -> str:
        return get_work_root(self.host.ssh_username)

    @classmethod
    async def get_required_mcp_servers(cls, subject: Any) -> tuple[RequiredMcpServer, ...]:
        # GitHub MCP acts as the review actor. The clone acts as the operator.
        actor = await get_review_actor()
        github = RequiredMcpServer(
            name=GITHUB_MCP_NAME,
            url=GITHUB_MCP_URL,
            secret_id=(await actor.service.get()).id,
            resource=cls.get_repo(subject),
        )
        if (await SoftwareFactory.settings()).tracker == "druks":
            return (github, get_druks_mcp_server(allowed_tools=TICKET_TOOLS))
        return (github,)

    async def run_agent(self, *, account_id: str | None, **kwargs: Any):
        # Agents clone related repos on demand. The --add-dir target must exist first.
        related_root = get_related_root(self.host.ssh_username)
        await self.host.exec(["mkdir", "-p", related_root], timeout=10.0)
        return await super().run_agent(account_id=account_id, **kwargs)

    def get_agent_run_kwargs(self, **kwargs: Any) -> dict[str, Any]:
        # Never the repo cwd. Claude hangs with no output on ``--add-dir <cwd>``.
        kwargs = super().get_agent_run_kwargs(**kwargs)
        kwargs["add_dirs"] = (get_related_root(self.host.ssh_username),)
        kwargs["skills"] = self.skills
        return kwargs


class Build(Workflow):
    subject = WorkItem
    steps_reuse_sandbox = True
    workspace_class = BuildWorkspace
    journal_class = BuildJournal
    journal: BuildJournal

    class Settings(BaseModel):
        plan_gate: PlanGate = Field(
            default="human",
            title="Plan gate",
            description="Choose who approves the plan before implementation.",
            json_schema_extra={
                "choice_details": {
                    "human": {
                        "label": "Human review",
                        "help": "You approve every plan. The machine reviewer does not run.",
                    },
                    "machine": {
                        "label": "Machine review",
                        "help": (
                            "The machine reviewer checks once. "
                            "Implementation starts without your approval."
                        ),
                    },
                    "machine_then_human": {
                        "label": "Machine then human",
                        "help": "The machine reviewer checks once. You then approve the plan.",
                    },
                    "adaptive": {
                        "label": "Adaptive review",
                        "help": (
                            "An approved high-confidence plan starts directly. "
                            "All other plans need your approval."
                        ),
                    },
                },
            },
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
            title="Code review",
            description="Include the advisory code-review lens in the implementation review.",
        )

    @classmethod
    async def dispatch(cls, *, ticket: dict) -> str | None:
        # A ticket at the trigger status opens a build. If a build is already active,
        # start() returns it and announces nothing, so the ticket takes its status.
        item = await WorkItem.get_for_ticket_key(
            source=ticket["source"], ticket_key=ticket["identifier"]
        )
        if item:
            if item.resolution == Resolution.MERGED:
                logger.info(
                    "Ticket %s is already merged. The redelivery does nothing.",
                    ticket["identifier"],
                )
                return
            await item.update(title=ticket["title"], ticket_url=ticket["url"])
            status = await item.get_status(workflow=cls)
            if status.is_active:
                reviewing = status.gate == ReviewWork.name
                await item.set_ticket_status(
                    TicketStatus.IN_REVIEW if reviewing else TicketStatus.IN_PROGRESS
                )
                return
        else:
            project_repo = await ProjectRepo.lookup(
                project_name=ticket["project_name"], labels=ticket["labels"]
            )
            if project_repo:
                item = await WorkItem.create(
                    project_id=project_repo.project_id,
                    source=ticket["source"],
                    title=ticket["title"] or ticket["identifier"],
                    ticket_key=ticket["identifier"],
                    ticket_url=ticket["url"],
                    repo=project_repo.full_name,
                )
            else:
                logger.info(
                    "Ticket %s has no routable repo. No build starts.", ticket["identifier"]
                )
                return
        try:
            await Github.get()
        except ServiceNotConnectedError as error:
            # The delivery succeeded. A raise would answer 5xx, and the provider would
            # deliver it again.
            logger.info("Ticket %s cannot start a build: %s", ticket["identifier"], error)
            await item.announce("build.rejected", reason=str(error))
            return
        account_id = None
        if ticket["assignee_id"] and (tracker := await SoftwareFactory.get_tracker()):
            async with tracker:
                account_id = await tracker.get_account_id(ticket["assignee_id"])
        return await cls.start(
            subject=item,
            account_id=account_id,
            assignee_email=ticket["assignee_email"],
            assignee_name=ticket["assignee_name"],
        )

    async def run_multistep(
        self,
        assignee_email: str | None = None,
        assignee_name: str | None = None,
    ) -> None:
        # Steps read the policy, the profile, and the settings, so replay reuses them.
        resolved = await self._load_policy_and_profile()
        self._policy = RepoPolicy.model_validate(resolved["policy"])
        self._profile = resolved["profile"]
        self._settings = await self._load_settings()

        if await self._plan_phase():
            await self._implement_phase()

    async def get_workspace_kwargs(self, host: "Host") -> dict[str, Any]:
        kwargs = await super().get_workspace_kwargs(host)
        kwargs = {
            **kwargs,
            # None until the first implement provisions the PR branch.
            "branch": self.branch,
            "skills": tuple(self._profile.get("recommended_skills", [])),
        }
        return kwargs

    async def get_prompt_context(self, **context: Any) -> dict[str, Any]:
        work_item = await self.subject
        project_repo = await ProjectRepo.get_for_repo(work_item.repo, raise_on_missing=True)
        endpoint = load_settings().urls.endpoint.rstrip("/")
        work_item_url = f"{endpoint}/software_factory/work-items/{work_item.id}" if endpoint else ""
        prompt_context = BuildPromptContext(
            repo=work_item.repo,
            work_item_url=work_item_url,
            branch=self.branch,
            pr_number=self.pr_number,
            ticket_ref=work_item.ticket_key,
            source=work_item.source,
            assignee_name=self.input.assignee_name,
            assignee_email=self.input.assignee_email,
            related_repos=await project_repo.siblings(),
            skills=await Skill.list_delivered(self._profile.get("recommended_skills", [])),
            review_code=self._settings.review_code,
            review_mode=(await get_review_actor()).mode,
            journal=self.journal,
        )
        return {
            "verification": await self._policy.verification_block(
                profile=self._profile, repo=work_item.repo
            ),
            "build": prompt_context,
            **await super().get_prompt_context(**context),
        }

    @step
    async def _load_policy_and_profile(self) -> dict[str, Any]:
        repo = (await self.subject).repo
        policy = await RepoPolicy.resolve(repo)
        project_repo = await ProjectRepo.get_for_repo(repo, raise_on_missing=True)
        return {
            "policy": policy.model_dump(mode="json"),
            "profile": project_repo.effective_profile,
        }

    @step
    async def _load_settings(self) -> "Build.Settings":
        # Replay reuses the settings that the run started with, not later edits.
        return await self.settings()

    async def _plan_phase(self) -> bool:
        """Return True when the plan is approved for implementation."""
        plan_gate = self._policy.plan_approval_gate(self._settings.plan_gate)
        answered_questions: list[dict[str, str]] = []
        operator_note = ""
        critique = ""
        reviewed = False
        while True:
            plan = await SoftwareFactory.generate_plan(
                answered_questions=answered_questions,
                operator_note=operator_note,
                reviewer_notes=critique,
            )
            critique = ""
            if not plan.questions:
                if plan_gate != "human" and not reviewed:
                    # The machine reviewer gets one pass per run. A critique goes into
                    # one redraft, which continues without a second review.
                    reviewed = True
                    machine_review = await SoftwareFactory.review_plan()
                    if machine_review.decision == ReviewDecision.REQUEST_CHANGES:
                        critique = machine_review.body
                        continue
                    if plan_gate == "machine":
                        return True
                    if (
                        plan_gate == "adaptive"
                        and plan.confidence == "high"
                        and plan.acceptance_criteria
                    ):
                        return True
                elif plan_gate == "machine":
                    return True
            operator_reply = await self.review(questions=plan.questions)
            if plan.is_confirmed_by(operator_reply):
                return True
            answered_questions = plan.get_answered_questions(operator_reply.answers)
            operator_note = operator_reply.note

    async def _implement_phase(self) -> None:
        while True:
            await self.implement()
            evaluation = await SoftwareFactory.evaluate_implementation()
            if evaluation.verdict == EvaluationVerdict.FAIL and (
                self.journal.implementation_revision < self._settings.max_implementation_revisions
            ):
                continue
            if await self._work_gate():
                return

    async def _work_gate(self) -> bool:
        """Park for work approval. Return True when the work is done, or False when it
        goes back to implementation."""
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
            await SoftwareFactory.revise_contract()
        return False

    async def _approved_work(self) -> bool:
        # GitHub announces the merge. The pr.closed reaction stores the result.
        if self._policy.on_approval == "merge":
            if await self.declare_merge_intent():
                return True
            logger.warning(
                "GitHub did not accept the merge of %s#%s. The work parks for review again.",
                (await self.subject).repo,
                self.pr_number,
            )
            return await self._work_gate()
        await self._clear_draft()
        return True

    async def _triage(self) -> bool:
        feedback = await SoftwareFactory.triage_human_feedback()
        if feedback.action == HumanFeedbackAction.CHANGE_REQUIRED:
            return False
        if feedback.action == HumanFeedbackAction.CONTRACT_CHANGE_REQUIRED:
            await SoftwareFactory.revise_contract()
            return False
        if feedback.action == HumanFeedbackAction.CLOSE:
            raise FatalError("closed at human triage")
        # NO_CHANGE and QUESTION park the work again.
        return await self._work_gate()

    # Body code, never @step. The agent calls inside memoize themselves and write the
    # journal. On replay a @step would skip them, and the journal would stay empty.
    async def implement(self) -> ImplementationOutput:
        delivery = await SoftwareFactory.implement()
        # The implementer found a contradiction in the binding requirements and stopped.
        # The run fails with that reason, so the dashboard shows it.
        if delivery.status == "needs_clarification":
            raise FatalError(f"implementation needs clarification: {delivery.summary}")
        if self.journal.implementation_revision == 1:
            # The first delivery also opened the branch and the draft PR.
            await self.announce("pr.opened", pr_number=delivery.pr_number, branch=delivery.branch)
        return delivery

    @step
    async def declare_merge_intent(self) -> bool:
        """Whether GitHub accepted ownership of the merge."""
        github = await get_github_client()
        return await github.merge_when_ready((await self.subject).repo, self.pr_number)

    # The branch and the PR come from the first delivery. Before it, both are None.
    # Rework deliveries reuse the pair. The item row and webhook routing use it as a
    # key, so a later delivery with other values must not change these reads.
    @property
    def branch(self) -> str | None:
        implementations = self.journal.implementations
        return implementations[0].branch if implementations else None

    @property
    def pr_number(self) -> int | None:
        implementations = self.journal.implementations
        return implementations[0].pr_number if implementations else None

    @step
    async def _clear_draft(self) -> None:
        await self.set_pr_draft(draft=False)

    async def request_assignee_review(self) -> None:
        login = self.journal.assignee_github_login
        repo = (await self.subject).repo
        if login and self.pr_number:
            try:
                await (await get_github_client()).request_pull_request_reviewers(
                    repo, self.pr_number, [login]
                )
            except Exception:  # noqa: BLE001 — a missed ping must not fail the park
                logger.warning(
                    "could not request review from %s on %s#%s",
                    login,
                    repo,
                    self.pr_number,
                )

    async def set_pr_draft(self, *, draft: bool) -> None:
        repo = (await self.subject).repo
        if self.pr_number:
            try:
                await (await get_github_client()).set_pull_request_draft_state(
                    repo, self.pr_number, draft=draft
                )
            except Exception:  # noqa: BLE001 — a draft merge fails loudly anyway
                logger.warning("Could not set draft=%s on %s#%s.", draft, repo, self.pr_number)


class ProfileWorkspace(RepoWorkspace):
    @classmethod
    def get_repo(cls, subject: Any) -> str:
        return subject.full_name


class Profile(Workflow):
    """Profile a repo once, when it joins a project. ``refresh_only`` skips the agent
    and applies the pinned verification to the stored baseline again."""

    subject = ProjectRepo
    workspace_class = ProfileWorkspace

    @classmethod
    async def dispatch(cls, project_repo: ProjectRepo, *, refresh_only: bool = False) -> str:
        # The profiler clones with the operator App token. The lookup raises a clear
        # error before the run starts a VM.
        await Github.get()
        return await cls.start(
            subject=project_repo,
            repo_id=project_repo.id,
            refresh_only=refresh_only,
        )

    async def run(self, repo_id: int, refresh_only: bool = False) -> None:
        project_repo = await ProjectRepo.get(repo_id)

        if refresh_only:
            baseline = project_repo.profile.get("baseline") or {}
        else:
            baseline = await SoftwareFactory.repo_profiler(repo=project_repo.full_name)
            # A skill can be disabled after the prompt renders, so read the enabled
            # skills again.
            enabled = {skill.name for skill in await Skill.list_enabled()}
            baseline["recommended_skills"] = [
                name for name in baseline["recommended_skills"] if name in enabled
            ]

        policy = await RepoPolicy.resolve(project_repo.full_name)
        effective = dict(baseline)
        if policy.verification:
            effective["verification"] = policy.verification.get_commands(
                detected=baseline.get("verification") or {}
            )
        await project_repo.set_profile(baseline=baseline, effective=effective)

    async def get_prompt_context(self, **context: Any) -> dict[str, Any]:
        return {
            "repo": (await ProjectRepo.get(self.input.repo_id)).full_name,
            "skills_catalog": [
                {"name": skill.name, "description": skill.description}
                for skill in await Skill.list_enabled()
            ],
            **await super().get_prompt_context(**context),
        }


class ReviewWorkspace(RepoWorkspace):
    # A checkout of the default branch, with room beside it for siblings. The reviewer
    # checks out the PR itself. The add_dirs grant needs the directory to exist.
    @classmethod
    async def get_secret_refs(cls, subject: Any) -> list[SecretRef]:
        # The review is authored under the review actor's identity.
        actor = await get_review_actor()
        return [
            SecretRef(
                name=Github.secret_name,
                secret_id=(await actor.service.get()).id,
                resource=cls.get_repo(subject),
            )
        ]

    @property
    def related_root(self) -> str:
        return get_related_root(self.host.ssh_username)

    async def run_agent(self, *, account_id: str | None, **kwargs: Any):
        await self.host.exec(["mkdir", "-p", self.related_root], timeout=10.0)
        return await super().run_agent(account_id=account_id, **kwargs)

    def get_agent_run_kwargs(self, **kwargs: Any) -> dict[str, Any]:
        kwargs = super().get_agent_run_kwargs(**kwargs)
        kwargs["add_dirs"] = (self.related_root,)
        return kwargs


class PullRequestReview(Workflow):
    """Review one pull request against a checkout of its repo and the repos around it.
    The reviewer reads, judges, and posts the review itself."""

    subject = PullRequest
    workspace_class = ReviewWorkspace

    @classmethod
    async def dispatch(cls, *, repo: str, pr_number: int, requested_by: str) -> str:
        # A separate review identity still clones with the operator App. The lookup
        # raises a clear error before the run starts a VM.
        await Github.get()
        # The review runs under the account with the requester's name. Without that
        # account, it runs under the default account.
        account = await Account.get_for_username(requested_by)
        return await cls.start(
            subject=PullRequest.get(repo, pr_number),
            account_id=account.id if account else None,
            requested_by=requested_by,
        )

    async def run(self, requested_by: str) -> None:
        await SoftwareFactory.review_pull_request()

    async def get_prompt_context(self, **context: Any) -> dict[str, Any]:
        project_repo = await ProjectRepo.get_for_repo(
            (await self.subject).repo, raise_on_missing=True
        )
        return {
            "siblings": await project_repo.siblings(),
            "review_mode": (await get_review_actor()).mode,
            **await super().get_prompt_context(**context),
        }
