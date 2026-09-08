import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, Field

from druks.accounts.models import Account, PersonalAccessToken
from druks.contrib.software_factory.contracts import ImplementationOutput, ReviewWork
from druks.contrib.software_factory.enums import (
    EvaluationVerdict,
    HumanFeedbackAction,
    ReviewDecision,
)
from druks.contrib.software_factory.models import ProjectRepo, WorkItem
from druks.contrib.software_factory.ticketing.enums import TicketStatus
from druks.core.apis.github import GITHUB, get_github_client
from druks.durable.enums import RunState
from druks.sandbox.datastructures import RequiredMcpServer
from druks.sandbox.layout import get_related_root, get_work_root
from druks.services.exceptions import ServiceNotConnectedError
from druks.services.models import ServiceIdentity
from druks.settings import load_settings
from druks.skills.models import Skill
from druks.workflows import FatalError, Workflow, step
from druks.workspaces import RepoWorkspace

from .app import SoftwareFactory
from .constants import APPLIANCE_MCP_NAME, GITHUB_MCP_NAME, GITHUB_MCP_URL
from .datastructures import PullRequest
from .github import get_review_actor
from .journal import BuildJournal
from .policy import PlanGate, RepoPolicy
from .prompt_context import TRACKER_LABELS, BuildPromptContext

if TYPE_CHECKING:
    from druks.sandbox.host import Host

logger = logging.getLogger(__name__)

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def appliance_mcp_url() -> str:
    """The appliance /mcp as a sandbox reaches this process. Loopback is this
    host, not the VM, so it becomes the Docker host gateway."""
    endpoint = load_settings().urls.endpoint.rstrip("/")
    if not endpoint:
        raise FatalError(
            "urls.endpoint is unset; the issues tracker tools need /mcp reachable from the sandbox."
        )
    parts = urlsplit(endpoint)
    host = parts.hostname or ""
    if host in _LOOPBACK_HOSTS:
        port = f":{parts.port}" if parts.port else ""
        endpoint = urlunsplit(
            (parts.scheme, f"host.docker.internal{port}", parts.path, "", "")
        ).rstrip("/")
    return f"{endpoint}/mcp"


@dataclass(frozen=True, kw_only=True)
class BuildWorkspace(RepoWorkspace):
    skills: tuple[str, ...]
    # Installation token for build's github MCP server, minted per repo from
    # the identity reviews act as. Required — there is no build without github.
    mcp_token: str
    # Appliance /mcp, set only when the tracker is issues. Empty otherwise —
    # Linear and Jira do not take this server.
    appliance_mcp_url: str = ""
    appliance_mcp_token: str = ""

    @property
    def workspace_root(self) -> str:
        return get_work_root(self.host.ssh_username)

    def get_required_mcp_servers(self) -> tuple[RequiredMcpServer, ...]:
        servers = (
            RequiredMcpServer(name=GITHUB_MCP_NAME, url=GITHUB_MCP_URL, token=self.mcp_token),
        )
        if self.appliance_mcp_url:
            servers += (
                RequiredMcpServer(
                    name=APPLIANCE_MCP_NAME,
                    url=self.appliance_mcp_url,
                    token=self.appliance_mcp_token,
                ),
            )
        return servers

    async def run_agent(self, *, account_id: str | None, **kwargs: Any):
        # Agents clone related repos on demand; Claude's --add-dir target must exist first.
        related_root = get_related_root(self.host.ssh_username)
        await self.host.exec(["mkdir", "-p", related_root], timeout=10.0)
        return await super().run_agent(account_id=account_id, **kwargs)

    def get_agent_run_kwargs(self, **kwargs: Any) -> dict[str, Any]:
        # Never the repo cwd — Claude wedges (no stdout, forever) on ``--add-dir <cwd>``.
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
        # The tracker funnel's entry: a ticket at the trigger status opens a build.
        # A live run already holds the queue slot — start() would return it
        # without announcing, so mirror the ticket onto that run instead.
        item = await WorkItem.get_for_ticket_key(
            source=ticket["source"], ticket_key=ticket["identifier"]
        )
        if item:
            if item.resolution == "merged":
                logger.info(
                    "Ticket %s is already merged; skipping redelivery.", ticket["identifier"]
                )
                return
            await item.update(title=ticket["title"], ticket_url=ticket["url"])
            status = await item.get_status(workflow=cls)
            if status.is_parked:
                await item.set_ticket_status(
                    TicketStatus.IN_REVIEW
                    if status.gate == ReviewWork.name
                    else TicketStatus.IN_PROGRESS
                )
                return
            if status.state in (RunState.SCHEDULED, RunState.RUNNING):
                await item.set_ticket_status(TicketStatus.IN_PROGRESS)
                return
        else:
            repo = await ProjectRepo.lookup(
                project_name=ticket["project_name"], labels=ticket["labels"]
            )
            if repo:
                item = await WorkItem.create(
                    project_id=repo.project_id,
                    source=ticket["source"],
                    title=ticket["title"] or ticket["identifier"],
                    ticket_key=ticket["identifier"],
                    ticket_url=ticket["url"],
                    repo=repo.full_name,
                )
            else:
                logger.info("Ticket %s has no routable repo; skipping.", ticket["identifier"])
                return
        try:
            await ServiceIdentity.get(GITHUB)
        except ServiceNotConnectedError as error:
            # A raise would 5xx the tracker's webhook and put the delivery into
            # provider redelivery; the delivery itself succeeded. Log the
            # Connect GitHub direction and stand down without starting.
            logger.info("Ticket %s cannot start a build: %s", ticket["identifier"], error)
            return
        email = ticket["assignee_email"]
        assignee = await Account.get_for_username(email.strip()) if email else None
        return await cls.start(
            subject=item,
            account_id=assignee.id if assignee else None,
            task_owner_email=email,
            task_owner_name=ticket["assignee_name"],
        )

    async def run_multistep(
        self,
        issue_number: int | None = None,
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

        if await self._plan_phase():
            await self._implement_phase()

    async def get_workspace_kwargs(self, host: "Host") -> dict[str, Any]:
        kwargs = await super().get_workspace_kwargs(host)
        repo = kwargs["subject"].repo
        try:
            mcp_token = await (await get_review_actor()).client.token_for_repo(repo)
        except Exception as error:
            # There is no build without github: agents push and review through
            # the github MCP, so a run that can't mint its token fails here,
            # loudly, instead of degrading mid-run.
            raise FatalError(
                f"Could not mint the GitHub token for {repo}; build requires it "
                "for its github MCP server."
            ) from error
        kwargs = {
            **kwargs,
            # None until the first implement provisions the PR branch.
            "branch": self.branch,
            "mcp_token": mcp_token,
            "skills": tuple(self._profile.get("recommended_skills", [])),
        }
        if (await SoftwareFactory.settings()).tracker == "issues":
            kwargs["appliance_mcp_url"] = appliance_mcp_url()
            account_id = self.account_id
            if account_id:
                account = await Account.get(account_id)
                if not account:
                    raise FatalError(
                        f"issues tracker tools need account {account_id} to mint the /mcp PAT."
                    )
            else:
                account = await Account.get_default()
                if not account:
                    raise FatalError(
                        "issues tracker tools need a run account or a default "
                        "account to mint the /mcp PAT."
                    )
            _, kwargs["appliance_mcp_token"] = await PersonalAccessToken.create(
                account_id=account.id, name="issues sandbox"
            )
        return kwargs

    async def get_prompt_context(self, **context: Any) -> dict[str, Any]:
        work_item = await self.subject
        target_repo = await ProjectRepo.get_for_repo(work_item.repo, raise_on_missing=True)
        endpoint = load_settings().urls.endpoint.rstrip("/")
        work_item_url = f"{endpoint}/software_factory/work-items/{work_item.id}" if endpoint else ""
        prompt_context = BuildPromptContext(
            repo=work_item.repo,
            work_item_url=work_item_url,
            branch=self.branch,
            pr_number=self.pr_number,
            ticket_ref=work_item.ticket_key,
            source=work_item.source,
            tracker_label=TRACKER_LABELS[work_item.source],
            issue_number=self.input.issue_number,
            task_owner_name=self.input.task_owner_name,
            task_owner_email=self.input.task_owner_email,
            related_repos=await target_repo.siblings(),
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
        # One memoized read: the live policy + the work item's repo profiled facts.
        repo = (await self.subject).repo
        policy = await RepoPolicy.resolve(repo)
        target = await ProjectRepo.get_for_repo(repo, raise_on_missing=True)
        return {
            "policy": policy.model_dump(mode="json"),
            "profile": target.effective_profile,
        }

    @step
    async def _load_settings(self) -> "Build.Settings":
        # A step so replay reuses the values the run started with, not later edits.
        return await self.settings()

    async def _plan_phase(self) -> bool:
        """True → implement."""
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
                    # The machine reviewer gets one pass per run; a critique is
                    # folded into one redraft that proceeds without re-review.
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
            if evaluation.verdict == EvaluationVerdict.PASS:
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
            await SoftwareFactory.revise_contract()
        return False

    async def _approved_work(self) -> bool:
        # GitHub announces the merge; the pr.closed reaction stores its verdict.
        if self._policy.on_approval == "merge":
            if await self.declare_merge_intent():
                return True
            logger.warning(
                "GitHub did not accept the merge of %s#%s; re-parking for review.",
                (await self.subject).repo,
                self.pr_number,
            )
            return await self._work_gate()
        await self._clear_draft()
        return True

    async def _triage(self) -> bool:
        feedback = await SoftwareFactory.triage_human_feedback()
        if feedback.action == HumanFeedbackAction.CHANGE_REQUIRED:
            return False  # loop → implement
        if feedback.action == HumanFeedbackAction.CONTRACT_CHANGE_REQUIRED:
            await SoftwareFactory.revise_contract()
            return False
        if feedback.action == HumanFeedbackAction.CLOSE:
            raise FatalError("closed at human triage")
        # NO_CHANGE / QUESTION → re-park
        return await self._work_gate()

    # Body code, never @step: the agent calls inside memoize themselves and land
    # on the journal a @step would skip rebuilding.
    async def implement(self) -> ImplementationOutput:
        delivery = await SoftwareFactory.implement()
        # A bail is a stop, not a result: the implementer hit a contradiction in the
        # binding requirements and couldn't deliver. Fail the run with its own reason,
        # read off the dashboard instead of dug out of the transcript.
        if delivery.status == "needs_clarification":
            raise FatalError(f"implementation needs clarification: {delivery.summary}")
        if self.journal.implementation_revision == 1:
            # First delivery: the implementer provisioned the branch + draft PR
            # alongside its commits — announce them onto the item.
            await self.announce("pr.opened", pr_number=delivery.pr_number, branch=delivery.branch)
        return delivery

    @step
    async def declare_merge_intent(self) -> bool:
        """Whether GitHub accepted ownership of the merge."""
        github = await get_github_client()
        return await github.merge_when_ready((await self.subject).repo, self.pr_number)

    # The provisioned branch + PR, pinned to the FIRST delivery — None until then
    # (planning runs against the default branch, and there is no PR to point at).
    # Rework deliveries reuse the pair; the item row and webhook routing key on it,
    # so a delivery reporting different numbers must not move these reads.
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
    def get_repo(self) -> str:
        return self.subject.full_name


class Profile(Workflow):
    """Profiles a repo once, when it joins a project: the repo_profiler agent
    reads the checkout and reports stack, verification commands, and recommended
    skills onto ProjectRepo.profile. ``refresh_only`` skips the agent — it
    re-applies the operator's pinned verification over the stored baseline, for
    the reaction to a .druks/software_factory/config.yml push."""

    subject = ProjectRepo
    workspace_class = ProfileWorkspace

    @classmethod
    async def dispatch(cls, repo: ProjectRepo, *, refresh_only: bool = False) -> str:
        # The profiler clones with an operator-App token, so resolve the
        # identity before the start spends a run and provisions a VM — the
        # raising lookup surfaces the actionable not-connected error.
        await ServiceIdentity.get(GITHUB)
        return await cls.start(
            subject=repo,
            repo_id=repo.id,
            refresh_only=refresh_only,
        )

    async def run(self, repo_id: int, refresh_only: bool = False) -> None:
        # Every dispatch site verifies the repo exists first; a build never
        # profiles a repo that isn't there.
        project_repo = await ProjectRepo.get(repo_id)

        if refresh_only:
            baseline = project_repo.profile.get("baseline") or {}
        else:
            baseline = await SoftwareFactory.repo_profiler(repo=project_repo.full_name)
            # The agent picks from the catalog it was handed, but a skill can be
            # disabled between prompt render and result — read the ground truth again.
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
    # The default-branch checkout (the reviewer checks the PR out itself) plus room
    # beside it for siblings; Claude's add_dirs grant needs the directory to exist.

    @property
    def related_root(self) -> str:
        return get_related_root(self.host.ssh_username)

    async def get_github_token(self) -> str:
        # The review is authored under the review actor's identity.
        return await (await get_review_actor()).client.token_for_repo(self.get_repo())

    async def run_agent(self, *, account_id: str | None, **kwargs: Any):
        await self.host.exec(["mkdir", "-p", self.related_root], timeout=10.0)
        return await super().run_agent(account_id=account_id, **kwargs)

    def get_agent_run_kwargs(self, **kwargs: Any) -> dict[str, Any]:
        kwargs = super().get_agent_run_kwargs(**kwargs)
        kwargs["add_dirs"] = (self.related_root,)
        return kwargs


class PullRequestReview(Workflow):
    """Reviews one pull request against a checkout of the repo it targets and the
    repos around it; the reviewer reads, judges, and posts the review itself."""

    subject = PullRequest
    workspace_class = ReviewWorkspace

    @classmethod
    async def dispatch(cls, *, repo: str, pr_number: int, requested_by: str) -> str:
        # Even a distinct review identity clones alongside the operator App, so
        # resolve the operator identity before the start spends a run and
        # provisions a VM — the raising lookup surfaces the actionable error.
        await ServiceIdentity.get(GITHUB)
        # Attribution follows the requester when druks knows them by that name; a
        # review asked for by someone with no account runs as the system's.
        account = await Account.get_for_username(requested_by)
        return await cls.start(
            subject=PullRequest.get(repo, pr_number),
            account_id=account.id if account else None,
            requested_by=requested_by,
        )

    async def run(self, requested_by: str) -> None:
        await SoftwareFactory.review_pull_request()

    async def get_prompt_context(self, **context: Any) -> dict[str, Any]:
        target = await ProjectRepo.get_for_repo((await self.subject).repo, raise_on_missing=True)
        return {
            "siblings": await target.siblings(),
            "review_mode": (await get_review_actor()).mode,
            **await super().get_prompt_context(**context),
        }
