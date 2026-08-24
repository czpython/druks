import asyncio
import contextlib
import random
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dbos import DBOS, StepOptions
from pydantic import BaseModel, ConfigDict

from druks.apps.registry import agents
from druks.database import db_session
from druks.durable.activity import set_run_phase
from druks.durable.engine import _step_engine, step_session
from druks.durable.exceptions import WorkflowError
from druks.durable.models import AgentCall, Artifact
from druks.harnesses.exceptions import HarnessError, Retry
from druks.harnesses.models import HarnessConnection
from druks.harnesses.registry import get_harness_for_model
from druks.prompts import render_prompt
from druks.sandbox import gate as sandbox_gate
from druks.sandbox.client import sandbox_client
from druks.sandbox.constants import MAX_AGENT_TIMEOUT_SECONDS
from druks.settings import load_settings
from druks.usage.models import UsageScrape
from druks.user_settings.models import SettingsOverride
from druks.workflows import _in_step, current_workflow

if TYPE_CHECKING:
    from druks.sandbox.datastructures import AgentResult
    from druks.workflows import Workflow
    from druks.workspaces import Workspace

__all__ = ["Agent", "AgentOutput"]

_QUOTA_FALLBACK_WAIT_SECONDS = 30 * 60
_QUOTA_MAX_WAIT_SECONDS = 6 * 60 * 60
_REAP_BEFORE_WAIT_SECONDS = 120


@contextlib.asynccontextmanager
async def _runner(
    workflow: "Workflow", host_id: str | None, workflow_id: str, step: str | None
) -> AsyncIterator["Workspace"]:
    # The agent always runs in a Workspace. A warm run attaches the run's held VM; the
    # rest get a fresh ephemeral VM. Either way workflow.get_workspace() turns the VM into
    # the runner — fresh per call, so nothing (connection or credential) is held across steps.
    if host_id:
        vm = sandbox_client.attach(host_id=host_id)
    else:
        vm = sandbox_client.ephemeral(idempotency_key=f"{workflow_id}:{step}")
    async with vm as box:
        yield await workflow.get_workspace(box)


class AgentOutput(BaseModel):
    """Base for an agent's structured output contract — the model an ``Agent``
    declares as its ``contract``. The harness sends the model's schema to OpenAI's
    strict structured-output validator on a Codex model, which requires every
    object node to set ``additionalProperties: false`` and list every property in
    ``required``: ``extra="forbid"`` gives the former, and declaring every field
    without a default (optionals as required-but-nullable ``X | None``) gives the
    latter — a field with a default 400s at runtime."""

    model_config = ConfigDict(extra="forbid")

    def to_result(self) -> Any:
        # What the caller gets back from an agent call: the validated output itself
        # by default. Override to map the strict agent output onto a looser domain
        # type — the seam that keeps the agent contract out of durable records,
        # applied by the call so no caller ever invokes it.
        return self

    def get_artifact(self) -> dict[str, str]:
        # The call's renderable output as {kind, title, content} — the platform persists
        # it after the call. Empty unless the contract produces a reviewable document.
        return {}


@dataclass(frozen=True)
class Agent:
    contract: type[AgentOutput]
    # Operator-tunable declared default: the model the agent runs unless it is
    # overridden (per agent or globally) in settings. A family token
    # (codex/claude) resolves to that family's operator-tunable model.
    model: str
    # Display label for the settings UI; ``id`` is shown when it's None.
    name: str | None = None
    # Short human-friendly blurb of what the agent does, shown in the settings UI.
    description: str = ""
    # The prompt template ``run`` renders. None for agents that build their
    # prompt inline and drive the harness themselves (planning) instead of
    # going through ``run``.
    prompt: str | None = None
    # Operator-tunable declared defaults, overridable per agent or globally.
    # None inherits the global default (effort; timeout in seconds).
    effort: str | None = None
    timeout: int | None = None
    # ``include_plugins=False`` skips the operator's plugin state for prompts
    # that hit no MCP server.
    include_plugins: bool = True
    # ``id`` is the agent's durable key (settings, timeline, registry): the attribute
    # name it's declared as, or an explicit ``id=`` for a standalone agent (a test, a
    # one-off). ``app`` is the owning App's name, read from the class in
    # __set_name__ to group the settings UI — blank for a standalone agent (no owner).
    id: str = field(default="", compare=False)
    app: str = field(init=False, compare=False, default="")

    def __post_init__(self) -> None:
        if self.id:  # an explicit id means a standalone agent — it registers itself now
            agents.register(self)

    def __set_name__(self, owner: type, attr: str) -> None:
        if self.id:  # explicit id: already registered in __post_init__
            return
        object.__setattr__(self, "id", attr)
        object.__setattr__(self, "app", owner.name)
        agents.register(self)

    # The effective settings, resolved through the override store: per-agent
    # override → the agent's declared value → the operator's global default.
    # ``run`` uses these; callers that drive the harness themselves call them
    # directly.
    def get_model_name(self) -> str:
        return SettingsOverride.agent_model(self.id, self.model).value

    def get_effort(self) -> str:
        harness = get_harness_for_model(self.get_model_name()).name
        return SettingsOverride.agent_effort(self.id, self.effort, harness).value

    def get_timeout(self) -> int:
        harness = get_harness_for_model(self.get_model_name()).name
        resolved = SettingsOverride.agent_timeout(self.id, self.timeout, harness).value
        # Capped so a single call always fits inside a fresh sandbox lease.
        return min(resolved, MAX_AGENT_TIMEOUT_SECONDS)

    async def __call__(self, **context: object) -> Any:
        """Run the agent — ``await Ship.implement(...)`` — as a durable step in the
        current workflow and return its parsed output. An agent run is always memoized —
        this picks which step does it: its own, or the @step it's already inside.
        workflow_id comes from the workflow context, not the caller; everything
        else (repo, …) is prompt context."""
        if not self.id:
            # Built loose — never assigned to an App, never given an explicit
            # id — so its settings, its registry entry and the call it would record
            # all key on nothing.
            raise WorkflowError(
                "an agent runs under its own id — declare it on an App, or "
                "pass id= for a standalone one"
            )
        workflow = current_workflow.get(None)
        if not workflow:
            raise WorkflowError(
                f"agent {self.id!r} can only run inside a workflow; standalone agent "
                "runs aren't supported yet"
            )

        async def _invoke() -> Any:
            return await self._run(workflow_id=workflow.workflow_id, **context)

        if _in_step.get():
            transient_retry_index = 0
            while True:
                try:
                    return await _invoke()  # the enclosing @step owns the session + memoizes it
                except HarnessError as error:
                    # Transient only, on the error's own schedule. Quota waits
                    # are hours; nothing that long belongs inside a held step,
                    # so those fail here instead of sleeping.
                    if error.retry is not Retry.TRANSIENT or transient_retry_index >= len(
                        error.retry_delays
                    ):
                        raise
                    delay = error.retry_delays[transient_retry_index]
                    transient_retry_index += 1
                    # Plain sleep: nothing checkpoints inside a step — a crash
                    # re-runs the whole enclosing step from attempt one anyway.
                    await asyncio.sleep(delay * random.uniform(0.9, 1.1))

        async def _do() -> Any:  # a standalone run is its own memoized step + session
            async with step_session():
                return await _invoke()

        async def _quota_retry_wait() -> float:
            # Runs as its own step: the body does no IO, and replay reuses the
            # recorded wait instead of re-reading the scrape.
            async with step_session():
                harness = get_harness_for_model(self.get_model_name())
                # The scrape belongs to the charged connection — its account
                # differs from the run's on fallback.
                connection = HarnessConnection.lookup(harness.name, workflow.account_id)
                scrape = UsageScrape.latest_for(harness.name, connection.account_id)
                if scrape:
                    now = datetime.now(UTC)
                    reset = scrape.soonest_reset_after(now)
                    if reset:
                        return (reset - now).total_seconds()
                # No scrape yet, or nothing ahead of now: a blind half hour.
                return _QUOTA_FALLBACK_WAIT_SECONDS

        transient_retry_index = 0
        quota_retried = False
        while True:
            try:
                result = await DBOS.run_step_async(
                    StepOptions(name=f"{workflow.kind}.agent.{self.id}"), _do
                )
            except HarnessError as error:
                if error.retry is Retry.TRANSIENT:
                    # One retry per delay on the error's own schedule; past it,
                    # the failure stands.
                    if transient_retry_index >= len(error.retry_delays):
                        raise
                    delay = error.retry_delays[transient_retry_index]
                    transient_retry_index += 1
                    # Jitter, so a provider outage doesn't wake every run at once.
                    wait_seconds = delay * random.uniform(0.9, 1.1)
                elif error.retry is Retry.QUOTA:
                    # One shot: sleep until the provider's window turns, read
                    # off the latest usage scrape.
                    if quota_retried:
                        raise
                    quota_retried = True
                    wait_seconds = await DBOS.run_step_async(
                        StepOptions(name=f"{workflow.kind}.agent.{self.id}.retry_wait"),
                        _quota_retry_wait,
                    )
                    # A far-off reset (an exhausted weekly window) isn't worth
                    # sleeping into — fail now.
                    if wait_seconds > _QUOTA_MAX_WAIT_SECONDS:
                        raise
                    wait_seconds += random.uniform(60, 120)
                else:
                    # Permanent or unclassified — never retried.
                    raise
                # A long sleep shouldn't hold a VM; the next attempt rebuilds
                # its workspace from git, like a gate park.
                if wait_seconds >= _REAP_BEFORE_WAIT_SECONDS:
                    await workflow._reap_run()
                # Durable: the end time checkpoints now, so a restart resumes
                # with the remaining wait, and replay skips it entirely.
                await DBOS.sleep_async(wait_seconds)
            else:
                # Body-level only, on both passes: in-step calls hit the early return
                # live and are skipped with their step on replay.
                workflow.journal.add(result)
                return result

    async def _run(self, *, workflow_id: str, **context: Any) -> Any:
        """The raw execution: provision or attach a host, record the AgentCall, run
        the harness. ``__call__`` handles the durable wrapping + nesting."""
        if not self.prompt:
            raise WorkflowError(f"agent {self.id!r} has no prompt template to render")
        model = self.get_model_name()
        harness = get_harness_for_model(model)
        workflow = current_workflow.get()
        # Refusing an unservable call here beats provisioning a VM and
        # 401ing mid-run.
        connection = HarnessConnection.lookup(harness.name, workflow.account_id)
        # Plain snapshots: the commits below expire the ORM row mid-flight.
        connection_id, charged_account_id = connection.id, connection.account_id
        # An agent call is a durability boundary — its effects don't roll back —
        # so commit here rather than hold the step's connection idle through the
        # minutes of provisioning and the run.
        db_session().commit()
        settings = load_settings()
        artifact_dir = settings.artifacts_dir / f"run-{workflow_id}"

        engine = _step_engine()
        call_id = harness.mint_run_id(None)

        # Registered for provisioning through execution — this connection's
        # rotation defers around it.
        async with sandbox_gate.use(connection_id, call_id):
            await set_run_phase("provisioning_vm")
            host_id = await workflow._ensure_host()

            # Record the call RUNNING once it has a host to run on (its id names
            # the on-disk transcript dir) so the live step shows while the agent
            # works, then finish it — or fail it if the run raised after
            # starting. A provisioning failure happens before this and records
            # no call.
            async with _runner(workflow, host_id, workflow_id, self.id) as runner:
                # Templates read the live workflow + the workspace the agent runs in,
                # alongside whatever the workflow's get_prompt_context composes.
                prompt_context = await workflow.get_prompt_context(**context)
                prompt_context.setdefault("workflow", workflow)
                prompt_context.setdefault("workspace", runner)
                prompt = await render_prompt(self.prompt, **prompt_context)
                await set_run_phase("agent_running")
                AgentCall.start(
                    engine,
                    call_id=call_id,
                    run_id=workflow_id,
                    model=model,
                    agent=self.id,
                    host_id=runner.host_id,
                    account_id=charged_account_id,
                )
                try:
                    result = await self._execute(
                        runner,
                        model,
                        prompt,
                        artifact_dir,
                        call_id,
                        connection_id,
                        account_id=workflow.account_id,
                    )
                except BaseException as error:
                    AgentCall.fail(engine, call_id=call_id, error=error)
                    raise
                AgentCall.finish(engine, call_id=call_id, result=result)

        if result.error:
            raise result.error

        output = self.contract.model_validate(result.output)
        if spec := output.get_artifact():
            Artifact.record(call_dir=artifact_dir / call_id, call_id=call_id, **spec)
        return output.to_result()

    async def _execute(
        self,
        runner: "Workspace",
        model: str,
        prompt: str,
        artifact_dir: Path,
        call_id: str,
        connection_id: str,
        *,
        account_id: str | None,
    ) -> "AgentResult":
        schema = self.contract.model_json_schema()
        return await runner.run_agent(
            account_id=account_id,
            model=model,
            prompt=prompt,
            schema=schema,
            agent=self.id,
            effort=self.get_effort(),
            timeout=self.get_timeout(),
            artifact_dir=artifact_dir,
            call_id=call_id,
            include_plugins=self.include_plugins,
            connection_id=connection_id,
        )
