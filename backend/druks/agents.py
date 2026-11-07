import asyncio
import contextlib
import functools
import random
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from dbos import DBOS, StepOptions
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from druks.apps.exceptions import AppBotError
from druks.apps.loader import resolve_app
from druks.apps.registry import agents, bots
from druks.browser.agent_browsers import AgentBrowser
from druks.browser.enums import BrowserClient
from druks.chat.bots.dependencies import BotUser
from druks.chat.enums import BotAccess
from druks.db import db_session
from druks.durable.activity import set_run_phase
from druks.durable.engine import _step_engine, step_session
from druks.durable.enums import RunState
from druks.durable.exceptions import WaitTimeout, WorkflowError
from druks.durable.models import AgentCall, Artifact, Run
from druks.exceptions import ObjectNotFound
from druks.files.datastructures import File
from druks.files.exceptions import FileUnavailableError
from druks.files.models import FileRecord
from druks.harnesses.config import AgentConfig, get_config
from druks.harnesses.exceptions import (
    HarnessError,
    HarnessInvalidOutputError,
    Retry,
)
from druks.prompts.resolver import get_template
from druks.sandbox import gate as sandbox_gate
from druks.sandbox.models import SecretRef
from druks.secrets.enums import SecretKind
from druks.settings import get_settings
from druks.usage.models import UsageScrape
from druks.workflows import _in_step, current_workflow

if TYPE_CHECKING:
    from druks.browser.sessions import BrowserSession
    from druks.harnesses.base import Harness
    from druks.sandbox.datastructures import AgentResult
    from druks.sandbox.host import Host
    from druks.services.base import ServiceField
    from druks.workspaces import Workspace

__all__ = ["Agent", "AgentOutput", "Bot", "BotUser"]

_QUOTA_FALLBACK_WAIT_SECONDS = 30 * 60
_QUOTA_MAX_WAIT_SECONDS = 6 * 60 * 60
_REAP_BEFORE_WAIT_SECONDS = 120
# The topic a run waiting out a quota window listens on for a changed provider or account.
_QUOTA_TOPIC = "agents.quota"

_in_after_agent: ContextVar[bool] = ContextVar("_in_after_agent", default=False)


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

    def to_artifact(self) -> dict[str, str]:
        # The call's renderable output as {kind, title, content} — the platform persists
        # it after the call. Empty unless the contract produces a reviewable document.
        return {}

    def to_event(self) -> dict[str, str]:
        """Return a topic and optional summary to record with the saved artifact. Empty
        opts out."""
        return {}


@dataclass(frozen=True)
class Agent:
    contract: type[AgentOutput]
    # Display label for the settings UI; the attribute name shows when it's None.
    name: str | None = None
    # Short human-friendly blurb of what the agent does, shown in the settings UI.
    description: str = ""
    # The prompt template ``run`` renders. None for agents that build their
    # prompt inline and drive the harness themselves (planning) instead of
    # going through ``run``.
    prompt: str | None = None
    # Declared run timeout in seconds, overridable per agent; None inherits the
    # harness default. How long a step may take is a fact about the task; the
    # model and the effort are the operator's, set in Settings.
    timeout: int | None = None
    # ``include_plugins=False`` skips the operator's plugin state for prompts
    # that hit no MCP server.
    include_plugins: bool = True
    # ``include_mcp=False`` gives the call no MCP server and its sandbox no
    # server entry, for an agent that reads untrusted content.
    include_mcp: bool = True
    # The service secrets the call's sandbox holds, ``(Acme.fields.api_key,)``.
    # Each is a placeholder that the secrets proxy swaps at the service's host.
    secrets: "tuple[ServiceField, ...]" = ()
    # The saved browser session the call drives, and the client Druks starts
    # beside Chrome for it. The call gets the ``browser`` MCP server beside the
    # servers it has anyway.
    # A session is mutable, so it stays out of the agent's hash.
    browser: "BrowserSession | None" = field(default=None, compare=False)
    browser_client: BrowserClient | None = None
    # ``id`` is the agent's durable key (settings, timeline, registry, step name):
    # ``<app>.<attribute>`` for an agent declared on an App, or the explicit ``id=``
    # of a standalone agent (a test, a one-off). ``app`` is the owning App's name,
    # read from the class in __set_name__ to group the settings UI — blank for a
    # standalone agent (no owner).
    id: str = field(default="", compare=False)
    app: str = field(init=False, compare=False, default="")
    _after_agent: list[Callable[..., Awaitable[str | None]]] = field(
        default_factory=list, init=False, compare=False, repr=False
    )

    def __post_init__(self) -> None:
        if self.browser and self.browser_client != BrowserClient.PINCHTAB:
            raise TypeError(
                "An agent with browser= needs browser_client=BrowserClient.PINCHTAB. "
                "Druks starts no other client for an agent call."
            )
        if self.browser_client and not self.browser:
            raise TypeError("Agent(browser_client=...) needs browser=, the session it drives.")
        for secret in self.secrets:
            declared = f"{secret.service.__name__}.fields.{secret.name}"
            if not secret.is_secret:
                raise TypeError(f"{declared} is not a secret field, so no sandbox holds it")
            if not secret.service.host:
                raise TypeError(
                    f"{declared} goes to a sandbox, so {secret.service.__name__} must declare "
                    "`host`: the one host the secret may be sent to"
                )
            if secret.service.secret_kind != SecretKind.STATIC:
                raise TypeError(
                    f"{declared} belongs to an App key, which issues tokens; no sandbox holds it"
                )
        if self.id:  # an explicit id means a standalone agent — it registers itself now
            agents.register(self)

    def __set_name__(self, owner: type, attr: str) -> None:
        if self.id:  # explicit id: already registered in __post_init__
            return
        object.__setattr__(self, "id", f"{owner.name}.{attr}")
        object.__setattr__(self, "app", owner.name)
        agents.register(self)

    @classmethod
    def get(cls, id: str) -> "Agent":
        """The agent registered as ``id``, ``<app>.<attribute>``, loading its app first."""
        if id not in agents:
            resolve_app(id.partition(".")[0])
        agent = agents.get(id)
        if not agent:
            raise ObjectNotFound("agent", {"id": id})
        return agent

    def after_agent(
        self, hook: Callable[..., Awaitable[str | None]]
    ) -> Callable[..., Awaitable[str | None]]:
        """Call ``hook(prompt=..., workspace=...)`` when this agent ends its turn. Text
        that it returns goes back to the agent as one more turn."""
        self._after_agent.append(hook)
        return hook

    async def get_config(self) -> AgentConfig:
        """How this agent runs for the current run's actor, read from settings now."""
        workflow = current_workflow.get(None)
        if not workflow:
            raise WorkflowError(f"agent {self.id!r} reads its config only inside a workflow")
        return await get_config(db_session(), self.id, workflow.account_id)

    async def __call__(
        self, *, contract: type[AgentOutput] | None = None, **context: object
    ) -> Any:
        """Run the agent — ``await SoftwareFactory.implement(...)`` — as a durable step in the
        current workflow and return its parsed output. An agent run is always memoized —
        this picks which step does it: its own, or the @step it's already inside.
        workflow_id comes from the workflow context, not the caller; everything
        else (repo, …) is prompt context. ``contract`` overrides the output
        contract for this call, including schema generation and validation."""
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
            return await self._run(workflow_id=workflow.workflow_id, contract=contract, **context)

        if _in_step.get():
            transient_retry_index = 0
            while True:
                try:
                    # The enclosing @step memoizes the call.
                    return await _invoke()
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

        async def _quota_retry_wait() -> dict[str, Any]:
            # Runs as its own step: replay reuses the recorded wait instead of
            # re-reading the scrape.
            async with step_session() as session:
                config = await get_config(session, self.id, workflow.account_id)
                provider_id = config.model.partition("/")[0]
                scrape = await UsageScrape.latest_for(
                    session, provider_id, config.charged_account_id
                )
            now = datetime.now(UTC)
            reset = scrape.soonest_reset_after(now) if scrape else None
            # No scrape yet, or nothing ahead of now: a blind half hour.
            seconds = (reset - now).total_seconds() if reset else _QUOTA_FALLBACK_WAIT_SECONDS
            # Jitter, so one window's reset doesn't wake every run at once.
            wait = seconds + random.uniform(60, 120)
            return {
                "credential": config.quota_holder,
                "seconds": seconds,
                "wait": wait,
                "ends_at": (now + timedelta(seconds=wait)).isoformat(),
            }

        async def _quota_wait_left(credential: str, ends_at: str) -> float:
            # Another credential retries now; its own limit, if any, fails the call
            # and earns that credential its own wait.
            async with step_session() as session:
                config = await get_config(session, self.id, workflow.account_id)
            if config.quota_holder != credential:
                return 0
            return (datetime.fromisoformat(ends_at) - datetime.now(UTC)).total_seconds()

        transient_retry_index = 0
        # The credentials whose window this call has waited out once already.
        quota_waited: set[str] = set()
        while True:
            try:
                result = await DBOS.run_step_async(
                    StepOptions(name=f"{workflow.kind}.agent.{self.id}"), _invoke
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
                    # Wait until the provider's window turns, read off the latest
                    # usage scrape, once per credential.
                    quota = await DBOS.run_step_async(
                        StepOptions(name=f"{workflow.kind}.agent.{self.id}.retry_wait"),
                        _quota_retry_wait,
                    )
                    # A credential waits once, and a far-off reset (an exhausted
                    # weekly window) isn't worth sleeping into — fail now.
                    if (
                        quota["credential"] in quota_waited
                        or quota["seconds"] > _QUOTA_MAX_WAIT_SECONDS
                    ):
                        raise
                    quota_waited.add(quota["credential"])
                    await set_run_phase(
                        "waiting_for_quota", ends_at=datetime.fromisoformat(quota["ends_at"])
                    )
                    wait_seconds = quota["wait"]
                    if wait_seconds >= _REAP_BEFORE_WAIT_SECONDS:
                        await workflow._reap_run()
                    # Each wait's length comes from a step, so replay takes the same turns.
                    while wait_seconds > 0:
                        try:
                            await workflow.wait(
                                _QUOTA_TOPIC, label="waiting_for_quota", ttl_seconds=wait_seconds
                            )
                        except WaitTimeout:
                            break
                        # A changed provider or account woke the wait.
                        wait_seconds = await DBOS.run_step_async(
                            StepOptions(name=f"{workflow.kind}.agent.{self.id}.quota_check"),
                            functools.partial(
                                _quota_wait_left, quota["credential"], quota["ends_at"]
                            ),
                        )
                    continue
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

    @classmethod
    async def wake_quota_waits(cls, session: AsyncSession) -> None:
        """Wake every run waiting out a quota window, so it reads its agent's provider and
        account again. Call it after the change commits."""
        running = await session.scalars(select(Run.id).where(Run.state == RunState.RUNNING.value))
        for run_id in running:
            if await DBOS.get_event_async(run_id, "waiting_on", timeout_seconds=0) == _QUOTA_TOPIC:
                await DBOS.send_async(run_id, {}, topic=_QUOTA_TOPIC)

    async def _run(
        self,
        *,
        workflow_id: str,
        contract: type[AgentOutput] | None = None,
        **context: Any,
    ) -> Any:
        """The raw execution: read what the call needs, provision or attach a host,
        run the harness, record the call. ``__call__`` handles the durable
        wrapping + nesting. Each read is a unit of work of its own, so no
        connection sits idle through the boot, the preparation, or the run."""
        if not self.prompt:
            raise WorkflowError(f"agent {self.id!r} has no prompt template to render")
        contract = contract or self.contract
        workflow = current_workflow.get()
        if db_session.registry.has():
            # An agent call is a durability boundary — its effects don't roll
            # back — so the enclosing step's work commits now instead of waiting
            # in an open transaction through the run.
            await db_session().commit()
        artifact_dir = get_settings().artifacts_dir / f"run-{workflow_id}"
        engine = _step_engine()

        async with step_session() as session:
            # Refusing an unservable call here beats provisioning a VM and
            # 401ing mid-run.
            config = await get_config(session, self.id, workflow.account_id)
            call_id = config.harness_class.mint_run_id(None)
            running = await AgentCall.lookup_running(session, workflow_id, self.id)
        model = config.model
        subscription_id = config.subscription.id if config.subscription else None
        api_key_id = config.api_key.id if config.api_key else None

        # Registered for provisioning through execution — the subscription's
        # rotation defers around it. A key never rotates.
        gate = contextlib.nullcontext()
        if subscription_id:
            gate = sandbox_gate.use(subscription_id, call_id)
        borrow = contextlib.nullcontext()
        if self.browser:
            borrow = AgentBrowser.lend(
                self.browser,
                self.browser_client,
                account_id=workflow.account_id,
                call_id=call_id,
            )
        async with gate, borrow as lent_browser:
            await set_run_phase("provisioning_vm")
            async with step_session() as session:
                # Everything the box and the prompt need, from rows, before the
                # box exists. The workspace carries it across.
                subject = await workflow.subject
                workspace_class = workflow.workspace_class
                workspace = workspace_class(subject)
                mcp_servers, mcp_secret_refs = (), []
                if self.include_mcp:
                    mcp_servers, mcp_secret_refs = await workspace_class.get_all_mcp_servers(
                        session, subject, workflow.account_id
                    )
                if lent_browser:
                    server, secret = lent_browser
                    mcp_servers = (*mcp_servers, server)
                    mcp_secret_refs = [*mcp_secret_refs, secret]
                service_refs = []
                for secret in self.secrets:
                    # An optional service that is not connected leaves its secrets out.
                    if secret.service.required or await secret.service.is_connected():
                        service_refs.append(
                            SecretRef(
                                name=secret.secret_name,
                                secret_id=(await secret.service.get()).id,
                                host=secret.service.host,
                            )
                        )
                refs = [
                    *config.secret_refs,
                    *service_refs,
                    *(await workspace_class.get_secret_refs(subject)),
                    *mcp_secret_refs,
                ]
                # A deleted or missing File argument stops the call before the box
                # exists; the workspace ships the rest once it is up, and the
                # template reads each as its in-VM path.
                for value in context.values():
                    for file in value if type(value) is list else [value]:
                        if type(file) is File:
                            record = await session.get(FileRecord, file.id)
                            if not record or record.deleted_at:
                                raise FileUnavailableError(f"file {file.id} is deleted or missing")
                prompt_context = await workflow.get_prompt_context(**context)
                # The override lookup reads the GitHub credential from a row.
                template = await get_template(self.prompt, repo=prompt_context.get("repo"))
                harness = config.harness_class(
                    model=config.model, fast_mode=config.fast_mode, effort=config.effort
                )
                manifest = await harness.get_manifest(
                    session,
                    mcp_servers=mcp_servers,
                    skills=await workspace_class.get_skills(subject),
                )
            # An agent browser's box holds the call's key, so the box belongs to
            # the call: it is never the warm box, and no other attempt resumes it.
            # The same goes for a call from an after-agent hook: the hooked agent holds it.
            scope = self.id
            host_id = None
            if self.browser or _in_after_agent.get():
                scope = call_id
            else:
                host_id = await workflow._lease_host(config, refs)

            # Record the call RUNNING once it has a host to run on (its id names
            # the on-disk transcript dir) so the live step shows while the agent
            # works, then finish it — or fail it if the run raised after
            # starting. A provisioning failure happens before this and records
            # no call.
            async with workflow._workspace(
                host_id,
                scope,
                refs,
                workspace=workspace,
                secrets=config.secrets,
                secrets_hash=config.secrets_hash,
            ) as runner:
                # The running call's box is this box when its identity found it again:
                # this attempt takes that call over, under its id, instead of starting
                # a second agent beside it. A box that is gone leaves the call to
                # start over on a new one.
                if running and running.sandbox_host_id == runner.host_id:
                    call_id = running.id
                    resume_from = running
                else:
                    resume_from = None

                context_on_box = await self._upload_files(prompt_context, runner.host, call_id)
                context_on_box.setdefault("workflow", workflow)
                context_on_box["workspace"] = runner
                prompt = await template.render_async(**context_on_box)
                hooks = {}
                if self._after_agent:
                    hooks["after_agent"] = functools.partial(
                        self._run_after_agent,
                        harness=config.harness_class,
                        prompt=prompt,
                        workspace=runner,
                    )
                if resume_from:
                    await set_run_phase("agent_running")
                    attempt = runner.host.resume_agent(
                        harness,
                        agent=self.id,
                        artifact_dir=artifact_dir,
                        call_id=call_id,
                        started_at=resume_from.started_at,
                        timeout=config.timeout,
                        hooks=hooks,
                    )
                else:
                    await runner.prepare()
                    await set_run_phase("agent_running")
                    await AgentCall.start(
                        engine,
                        call_id=call_id,
                        run_id=workflow_id,
                        model=model,
                        agent=self.id,
                        host_id=runner.host_id,
                        subscription_id=subscription_id,
                        api_key_id=api_key_id,
                    )
                    attempt = runner.run_agent(
                        harness=harness,
                        manifest=manifest,
                        timeout=config.timeout,
                        identity=config.identity,
                        include_apps=config.include_apps,
                        agent=self.id,
                        prompt=prompt,
                        schema=contract.model_json_schema(),
                        artifact_dir=artifact_dir,
                        call_id=call_id,
                        include_plugins=self.include_plugins,
                        mcp_servers=mcp_servers,
                        hooks=hooks,
                    )
                try:
                    result = await attempt
                except BaseException as error:
                    await AgentCall.fail(engine, call_id=call_id, error=error)
                    raise
                if result.error:
                    await AgentCall.finish(engine, call_id=call_id, result=result)
                    raise result.error
                try:
                    output, workspace_files = self._parse_output(contract, result)
                    if workspace_files:
                        # The files come off the box, so they are recorded while it is up.
                        async with step_session() as session:
                            await runner.host.save_files(
                                session,
                                workspace_files,
                                app=workflow.app,
                                agent_call_id=call_id,
                            )
                    event, artifact = self._get_event_and_artifact(contract, output)
                    await AgentCall.finish(engine, call_id=call_id, result=result)
                except BaseException as error:
                    await AgentCall.fail(engine, call_id=call_id, error=error)
                    raise

        if artifact:
            async with step_session() as session:
                await Artifact.record(
                    session,
                    call_dir=artifact_dir / call_id,
                    call_id=call_id,
                    event=event,
                    **artifact,
                )
        return output.to_result()

    async def _upload_files(
        self, context: dict[str, Any], host: "Host", call_id: str
    ) -> dict[str, Any]:
        # Each File argument lands in the call's directory on the box and reads
        # as its path there, at the top level or inside a list.
        uploaded = {}
        for key, value in context.items():
            if type(value) is File:
                uploaded[key] = await value.upload(host, call_id)
            elif type(value) is list:
                uploaded[key] = [
                    await item.upload(host, call_id) if type(item) is File else item
                    for item in value
                ]
            else:
                uploaded[key] = value
        return uploaded

    def _parse_output(
        self, contract: type[AgentOutput], result: "AgentResult"
    ) -> tuple[AgentOutput, list[File]]:
        workspace_files: list[File] = []
        try:
            output = contract.model_validate(
                result.output, context={"workspace_files": workspace_files}
            )
        except ValidationError as error:
            raise HarnessInvalidOutputError(
                f"agent {self.id!r} returned a payload that fails {contract.__name__}: {error}"
            ) from error
        return output, workspace_files

    def _get_event_and_artifact(
        self, contract: type[AgentOutput], output: AgentOutput
    ) -> tuple[dict[str, str], dict[str, str]]:
        event = output.to_event()
        artifact = output.to_artifact()
        if event and not artifact:
            raise WorkflowError(
                f"{contract.__name__}.to_event() returned an event without an "
                "artifact. Implement to_artifact() for the saved result."
            )
        return event, artifact

    async def _run_after_agent(
        self, *, harness: type["Harness"], prompt: str, workspace: "Workspace"
    ) -> str:
        # The hooks run inside the hooked call's step, so an agent call in a hook
        # replays with that step instead of taking a checkpoint of its own.
        in_step = _in_step.set(True)
        in_after_agent = _in_after_agent.set(True)
        try:
            # The hooks run in the box work, through its session.
            texts = [await hook(prompt=prompt, workspace=workspace) for hook in self._after_agent]
        finally:
            _in_step.reset(in_step)
            _in_after_agent.reset(in_after_agent)
        return harness.get_after_agent_reply("\n\n".join(text for text in texts if text))


@dataclass(frozen=True)
class Bot:
    """An app's ``<app>.bot`` agent; access selects who writes and owns the conversation.
    Each tool names the ``operation_id`` of an app route tagged ``bot``."""

    prompt: str
    access: BotAccess = BotAccess.PUBLIC
    user_tools: tuple[str, ...] = ()
    admin_tools: tuple[str, ...] = ()
    name: str | None = None
    description: str = ""
    # The longest a turn may run, in seconds. None inherits the default.
    timeout: int | None = None
    id: str = field(default="", init=False, compare=False)
    app: str = field(default="", init=False, compare=False)

    def __set_name__(self, owner: type, attribute: str) -> None:
        if attribute == "bot":
            object.__setattr__(self, "id", f"{owner.name}.bot")
            object.__setattr__(self, "app", owner.name)
            bots.register(self)
            return
        raise AppBotError(
            f"app {owner.name!r} declares a Bot as {attribute!r}. An app has one Bot, "
            "and Druks reads it from the bot attribute. Name the attribute bot."
        )
