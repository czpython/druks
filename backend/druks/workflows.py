import inspect
from collections.abc import Callable
from contextlib import nullcontext, suppress
from contextvars import ContextVar
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Self, TypeVar, get_args, get_type_hints

from croniter import croniter
from dbos import DBOS, SetEnqueueOptions, SetWorkflowAttributes, SetWorkflowID, StepOptions
from dbos._dbos import _get_dbos_instance
from dbos._error import (
    DBOSAwaitedWorkflowCancelledError,
    DBOSQueueDeduplicatedError,
    DBOSWorkflowCancelledError,
)
from pydantic import BaseModel, Field, create_model
from uuid_utils import uuid7

from druks.accounts.context import current_account_id
from druks.durable.activity import set_run_phase
from druks.durable.datastructures import Subject
from druks.durable.engine import _step_engine, register_schedule, run_queue, step_session
from druks.durable.enums import AgentCallStatus, RunState, WorkflowEvent
from druks.durable.exceptions import FatalError, GateTimeout, SubjectlessGate, WorkflowError
from druks.durable.models import AgentCall, Run
from druks.durable.schemas import (
    AgentCallResponse,
    RunResponse,
    SubjectActivity,
    SubjectStatus,
    SubjectSummary,
)
from druks.events.models import Event
from druks.extensions.loader import resolve_workflow_extension
from druks.extensions.registry import workflows
from druks.extensions.settings import (
    coerce_setting_value,
    validate_setting_override,
    validate_settings_declaration,
)
from druks.harnesses.exceptions import HarnessError
from druks.models import StoredSubject, snake_name
from druks.notifications.outbox import notifications_queue, send_notification
from druks.sandbox.client import sandbox_client
from druks.sandbox.constants import SANDBOX_HOST_ROTATE_BEFORE_SECONDS
from druks.sandbox.datastructures import Workspace
from druks.signals import publish
from druks.user_settings.models import SettingsOverride, UserSettings

# druks.workflows is the author door for workflow authoring: the bases (Workflow,
# Gate, step) defined below plus the author-facing read contracts. The engine
# itself (druks.durable) stays internal.
__all__ = [
    "AgentCall",
    "AgentCallResponse",
    "AgentCallStatus",
    "FatalError",
    "Gate",
    "Journal",
    "OperatorReply",
    "RunResponse",
    "Subject",
    "SubjectActivity",
    "SubjectStatus",
    "SubjectSummary",
    "Workflow",
    "WorkflowError",
    "WorkflowEvent",
    "set_run_phase",
    "step",
]

if TYPE_CHECKING:
    from druks.sandbox.host import Sandbox

# A human gate can park for days; a long recv TTL still caps zombie parks.
GATE_TTL_SECONDS = 14 * 24 * 60 * 60

# The decision verbs an in-app review offers. Framework-owned: an extension's
# agent supplies the plan and questions (content), but the verbs are ours — so a
# resume can only carry an action we defined, the line the resume endpoint checks.
_ReviewAction = Literal["approve", "request_changes"]
_REVIEW_CONTROLS = get_args(_ReviewAction)

T = TypeVar("T")

# The running workflow instance, so a Gate's on_wait() can reach its extension's
# side-effects (set draft, request review, …) when the gate parks. No default:
# Gate.wait() only runs inside a workflow, so it's always set there.
current_workflow: ContextVar["Workflow"] = ContextVar("current_workflow")
# True while a @step body runs. An agent run inside one is already memoized by that
# step, so it skips wrapping itself; outside, it wraps itself in its own step.
_in_step: ContextVar[bool] = ContextVar("_in_step", default=False)

# Reserved so _entry's arity and old checkpoints stay untouched; a body
# parameter may not claim it.
_ACCOUNT_INPUT_KEY = "__account_id__"


def _resolve_body_method(cls: type["Workflow"]) -> str:
    # Stamps run() @step here, before _wrap_steps runs, so it picks it up.
    has_run = "run" in cls.__dict__
    has_multistep = "run_multistep" in cls.__dict__
    if has_run and has_multistep:
        raise WorkflowError(
            f"{cls.__name__} defines both run() and run_multistep() — exactly "
            "one is allowed: run() for a single durable operation, "
            "run_multistep() for orchestration across steps/gates."
        )
    if has_run:
        method = cls.__dict__["run"]
        if getattr(method, "_durable_step", False):
            raise WorkflowError(
                f"{cls.__name__}.run() doesn't take @step — the whole call is "
                "already the durable step. Remove the decorator."
            )
        method._durable_step = True
        method._step_name = None
        return "run"
    if has_multistep:
        method = cls.__dict__["run_multistep"]
        if getattr(method, "_durable_step", False):
            raise WorkflowError(
                f"{cls.__name__}.run_multistep() must not be @step — step the "
                "individual operations it calls instead, or use run() if the "
                "whole workflow is one operation."
            )
        return "run_multistep"
    raise WorkflowError(
        f"{cls.__name__} must define run() (a single durable operation) or "
        "run_multistep() (orchestration across multiple steps/gates)."
    )


class _DeclaredSubject:
    """``subject = WorkItem`` on a workflow: the kind of thing its runs are about, read
    off the workflow, and the particular one a run is about, read off that run."""

    def __init__(self, subject_class: "type[Subject] | type[StoredSubject] | None") -> None:
        self.subject_class = subject_class

    def __get__(self, run: "Workflow | None", owner: type) -> Any:
        if run is None:
            return self.subject_class
        # Live, not a snapshot taken at dispatch: a long-parked run resumes against
        # whatever the declared class says then, and finds nothing if it went away.
        if not run._subject:
            return
        return self.subject_class.get_for_subject_id(str(run._subject["id"]))


def _declare_subject(cls: type["Workflow"]) -> None:
    # The author writes a plain class attribute; it becomes the descriptor above.
    # Presence in ``cls.__dict__``, not the value: an explicit ``subject = None`` is a
    # declaration of nothing, which the error below names rather than passing over.
    if "subject" not in cls.__dict__:
        return
    declared = cls.__dict__["subject"]
    if not (isinstance(declared, type) and issubclass(declared, Subject | StoredSubject)):
        raise WorkflowError(
            f"{cls.__name__}.subject must be a Subject or StoredSubject subclass — a run "
            f"is about a kind of thing, not {declared!r}. A workflow about nothing "
            "declares no subject at all."
        )
    cls.subject = _DeclaredSubject(declared)


def _input_model_from_signature(cls: type["Workflow"]) -> type[BaseModel] | None:
    # A workflow's input IS its body's signature: plain annotated parameters,
    # Python's native way to declare inputs. The SDK synthesizes a pydantic
    # model from them (the wire contract) — start() validates kwargs against it
    # and dumps to JSON (it crosses a JSONB row and a DBOS checkpoint; never a
    # live or pickled object), and the entry re-validates. A parameter without a
    # default is required at start(); a cron-scheduled workflow must default
    # every parameter.
    method_name = cls._body_method
    method = getattr(cls, method_name)
    parameters = [p for name, p in inspect.signature(method).parameters.items() if name != "self"]
    if not parameters:
        return
    hints = get_type_hints(method)
    fields: dict[str, Any] = {}
    for p in parameters:
        if p.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            raise WorkflowError(f"{cls.__name__}.{method_name}() cannot take *args/**kwargs")
        if p.name not in hints:
            raise WorkflowError(f"{cls.__name__}.{method_name}() parameter {p.name!r} needs a type")
        default = ... if p.default is inspect.Parameter.empty else p.default
        fields[p.name] = (hints[p.name], default)
    return create_model(f"{cls.__name__}Input", **fields)


class Journal:
    """The run's working memory. Druks adds each body-level agent output and
    gate reply; a body adds its own derived values; subclass it for named
    projections and declare yours via ``journal_class``."""

    def __init__(self) -> None:
        self._entries: list[Any] = []

    def add(self, entry: Any) -> None:
        self._entries.append(entry)

    def filter(self, contract: type[T], *, after: Any = None, **filters: Any) -> list[T]:
        # ``after`` anchors by identity: only entries recorded after that exact entry.
        entries = self._entries
        if after:
            if positions := [i for i, entry in enumerate(entries) if entry is after]:
                entries = entries[positions[0] + 1 :]
            else:
                raise WorkflowError("after= must be an entry of this journal")
        return [
            entry
            for entry in entries
            if isinstance(entry, contract)
            and all(getattr(entry, name) == expected for name, expected in filters.items())
        ]

    def latest(self, contract: type[T], **filters: Any) -> T | None:
        with suppress(IndexError):
            return self.filter(contract, **filters)[-1]
        return


class Gate(BaseModel):
    """A typed human-in-the-loop gate. Subclass per park point; ``name`` pins the
    gate's durable identity — the recv channel and the parked run's ``gate`` on
    the read side — and the fields are the reply's schema. `wait()` parks the
    running workflow until `answer()` resolves it (or the TTL lapses) and
    returns the validated reply — both ends of the channel hang off the class."""

    name: ClassVar[str] = ""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if not cls.__dict__.get("name"):
            raise WorkflowError(
                f"{cls.__name__} must pin ``name`` — it is the gate's durable identity "
                "(the recv channel and the parked run's gate), so it never rides the "
                "class name."
            )

    @classmethod
    async def on_wait(cls, workflow: "Workflow") -> None:
        # Runs when the gate parks, before the run suspends — override to make a
        # human aware there's something to answer (set PR draft, request review,
        # notify Slack, …). Default: nothing.
        return

    @classmethod
    async def answer(cls, subject: Subject | StoredSubject, **reply: Any) -> None:
        if not isinstance(subject, Subject | StoredSubject):
            raise WorkflowError(
                f"{cls.__name__}.answer() takes the subject whose run is parked on it, "
                f"not {type(subject).__name__}"
            )
        runs = Run.list_for_subject(subject.subject_type, str(subject.id))
        parked = next((run for run in runs if run.is_parked and run.input_gate == cls.name), None)
        if parked:
            await parked.resume(**reply)
            return
        raise WorkflowError(
            f"subject {subject.subject_type}:{subject.id} is not parked on gate {cls.name!r}"
        )

    @classmethod
    async def wait(
        cls, *, input_request: dict[str, Any] | None = None, ttl_seconds: float = GATE_TTL_SECONDS
    ) -> Self:
        # Suspend the running workflow until its gate is answered. A gate is a
        # run-level state — the read surfaces "needs you" straight off the parked run.
        # ``input_request`` is the plain-dict ask (at least a ``label`` and
        # ``presentation``), stored on the run beside ``input_gate`` and cleared on
        # resume — so an extension declares the ask here, beside on_wait, not at read time.
        workflow = current_workflow.get()
        if not workflow._subject and cls.on_wait.__func__ is Gate.on_wait.__func__:
            # No subject means no feed surface; if on_wait wasn't overridden
            # either, nobody would ever see this park — fail loudly instead
            # of parking invisibly for the whole TTL.
            raise SubjectlessGate(cls.name)

        async def _on_wait() -> None:
            # on_wait is real IO (it tells a human), so it gets its own checkpointed
            # step with a DB session.
            async with step_session():
                await cls.on_wait(workflow)

        await DBOS.run_step_async(StepOptions(name=f"{cls.name}._on_wait"), _on_wait)
        payload = await _park(workflow, cls.name, input_request, ttl_seconds)
        reply = cls.model_validate(payload)
        workflow.journal.add(reply)
        return reply


class OperatorReply(Gate):
    """The operator's in-app review decision. Answers and note are content for
    the next agent prompt, never control flow."""

    # One gate serves every in-app review — a run parks on one at a time.
    name = "review"
    action: _ReviewAction
    answers: dict[str, str] = Field(default_factory=dict)
    note: str = ""


async def _park(
    workflow: "Workflow",
    gate: str,
    input_request: dict[str, Any] | None,
    ttl_seconds: float,
) -> dict[str, Any]:
    # Shared park core: a park lasts days, so reap the warm VM, then suspend on the
    # gate's channel until Run.resume answers it.
    await workflow._reap_run()
    await _emit_run_event(
        workflow.workflow_id,
        RunState.PARKED,
        subject=workflow._subject,
        facts={
            "input_gate": gate,
            "input_request": input_request,
            "input_requested_at": datetime.now(UTC),
        },
    )
    if workflow._subject:
        # Every subjected park notifies the designated destination — no author opt-in.
        await _notify_designated_destination(workflow.workflow_id, workflow._subject)
    payload = await DBOS.recv_async(gate, timeout_seconds=ttl_seconds)
    if payload is None:
        raise GateTimeout(gate)
    await _emit_run_event(
        workflow.workflow_id,
        RunState.RUNNING,
        subject=workflow._subject,
        facts={**_GATE_CLEARED, "answer_parked_at": Run.input_requested_at},
    )
    return payload


async def _notify_designated_destination(workflow_id: str, subject: dict[str, Any]) -> None:
    # Reads the ask off the run row the parked step just wrote (the
    # signal payload carries no ask — producer-side placement is the point);
    # the settings pointer is the operator's off-switch.
    async def _create() -> str | None:
        async with step_session():
            destination_id = UserSettings.get().gate_park_destination_id
            if destination_id:
                return Run.get(workflow_id).create_park_notification(destination_id, subject)

    notification_id = await DBOS.run_step_async(
        StepOptions(name="notifications.gate_park", **_IO_RETRIES), _create
    )
    if not notification_id:
        return
    # The step memoized the row (one per parked round); this body-level enqueue
    # is DBOS's deterministic child-start, so a replayed park never double-sends.
    await notifications_queue.enqueue_async(send_notification, notification_id)


def step(method: Callable | None = None, *, name: str | None = None) -> Callable:
    """Mark a ``run_multistep()`` helper method as a durable, replay-safe step —
    its body runs once and its return is memoized for recovery. `name=` pins the
    durable step name independent of the method name. A leading `_` carries no
    semantics — it's just Python privacy."""

    def stamp(m: Callable) -> Callable:
        m._durable_step = True  # type: ignore[attr-defined]
        m._step_name = name  # type: ignore[attr-defined]
        return m

    return stamp(method) if method else stamp


# Lifecycle steps do real IO (DB, Redis, event subscribers), so a
# transient failure must not become a failed run — a lost reaction has no
# redelivery, unlike a webhook.
_IO_RETRIES: StepOptions = {"retries_allowed": True, "max_attempts": 5}


async def _emit_run_event(
    workflow_id: str,
    state: RunState,
    *,
    subject: dict[str, Any] | None,
    facts: dict[str, Any] | None = None,
    result: Any = None,
) -> None:
    # Facts (gate, ask, failure — DBOS owns state) and the event commit in one
    # memoized step; the signal publishes in a second, so a raising subscriber
    # can't roll back the record. Publish is at-least-once and can land before
    # DBOS commits the terminal status — subscribers stay idempotent and read
    # the payload, never derived Run.state. subject comes from the workflow's
    # own arguments, so a replay stamps the same routing every time.
    async def _transition() -> dict[str, Any] | None:
        async with step_session() as session:
            run = Run.get(workflow_id)
            if facts:
                for field, value in facts.items():
                    setattr(run, field, value)
                session.flush()
            # Subjectless framework crons are plumbing: no feed entry.
            if subject:
                return {
                    "kind": run.kind,
                    "subject": subject,
                    "payload": _log_run_event(run, state, subject, result),
                }

    transition = await DBOS.run_step_async(
        StepOptions(name=f"run.{state.value}", **_IO_RETRIES), _transition
    )
    if not transition:
        return

    async def _propagate() -> None:
        async with step_session():
            await publish(
                WorkflowEvent.for_state(state),
                subject=transition["subject"],
                kind=transition["kind"],
                **{k: v for k, v in transition["payload"].items() if k != "kind"},
            )

    await DBOS.run_step_async(
        StepOptions(name=f"run.{state.value}:propagate", **_IO_RETRIES), _propagate
    )


def _log_run_event(
    run: Run,
    state: RunState,
    subject: dict[str, Any],
    result: Any = None,
) -> dict[str, Any]:
    # One event per transition — the feed's run-level granularity, read off the
    # just-written row so gate and failure ride the transition that set them.
    # The result rides the finished event so reactions read the outcome off the
    # payload instead of artifacts.
    payload: dict[str, Any] = {"run": run.id, "kind": run.kind}
    if run.input_gate:
        payload["gate"] = run.input_gate
    if run.failure:
        payload["failure"] = run.failure
    if isinstance(result, BaseModel):
        payload["result"] = result.model_dump(mode="json")
    elif isinstance(result, dict):
        payload["result"] = result
    Event.emit(
        type=WorkflowEvent.for_state(state),
        subject=subject,
        label=run.subject_label,
        payload=payload,
        extension=workflows.get(run.kind).extension,
    )
    return payload


# Park sets the gate pair together; resume and a failure clear it together, so
# a terminal or resumed run never keeps a stale ask.
_GATE_CLEARED: dict[str, Any] = {"input_gate": None, "input_request": None}


async def _execute_run(
    workflow_id: str,
    kind: str,
    subject: dict[str, Any] | None,
    account_id: str | None,
    body: Callable,
) -> Any:
    # Ensure the row (idempotent, so a scheduled run with no start() makes it
    # here), then run the body between its running and finished/failed events.
    # Every failure re-raises so DBOS records the terminal ERROR derived state
    # reads; an operator cancel already carries its own reason and terminal
    # status, so it passes through untouched.
    Run.create_row(_step_engine(), workflow_id=workflow_id, kind=kind, account_id=account_id)
    await _emit_run_event(workflow_id, RunState.RUNNING, subject=subject)

    async def record_failed(exc: BaseException, code: str) -> None:
        facts = {**_GATE_CLEARED, "failure": str(exc), "failure_code": code}
        await _emit_run_event(workflow_id, RunState.FAILED, subject=subject, facts=facts)

    try:
        result = await body()
    except (DBOSAwaitedWorkflowCancelledError, DBOSWorkflowCancelledError):
        raise
    except (FatalError, HarnessError) as exc:
        await record_failed(exc, exc.code)
        raise
    except Exception as exc:
        await record_failed(exc, "")
        raise
    await _emit_run_event(workflow_id, RunState.FINISHED, subject=subject, result=result)
    return result


class Workflow:
    kind: ClassVar[str] = ""
    # The extension that declares this workflow — class identity, resolved from
    # the loader's package registrations at definition time and namespacing
    # ``kind``. Never supplied or stored per run.
    extension: ClassVar[str | None] = None
    # What this workflow's runs are about, written ``subject = WorkItem`` on the
    # subclass. None for a workflow about nothing, which says so by silence.
    subject = _DeclaredSubject(None)
    # When set to a cron string, the workflow also registers a schedule that
    # fires its run() on that cadence (no subject — a framework cron).
    every: ClassVar[str | None] = None
    # True holds one warm VM across the run's agent calls (released at gate parks);
    # False gives each call a throwaway VM.
    steps_reuse_sandbox: ClassVar[bool] = False
    # The Workspace subclass agents run in; an extension sets it (default: the bare VM).
    workspace_class: ClassVar[type[Workspace]] = Workspace
    # The Journal subclass the run keeps; an extension sets it for named projections.
    journal_class: ClassVar[type[Journal]] = Journal
    # Exactly one: run() is a single operation, auto-stepped, no ceremony.
    # run_multistep() orchestrates explicit @step calls and/or gates.
    run: ClassVar[Callable]
    run_multistep: ClassVar[Callable]
    _entry: ClassVar[Callable]
    # Which of run()/run_multistep() this subclass defines, resolved once at
    # class-definition time; _run_instance() calls it by name.
    _body_method: ClassVar[str] = "run"
    # The resolved body's declared input model (None for an input-less run),
    # re-validated from the wire per instance.
    _run_input_model: ClassVar[type[BaseModel] | None] = None

    class Settings(BaseModel):
        # Operator-tunable settings; a workflow overrides this inner class to
        # declare fields, none by default.
        pass

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        try:
            cls.extension = resolve_workflow_extension(cls.__module__)
        except LookupError:
            raise WorkflowError(
                f"{cls.__module__} declares workflow {cls.__name__} outside every "
                "registered extension package — workflow modules load through "
                "druks.extensions.loader; a module the loader doesn't own must "
                "register_workflow_package() before importing"
            ) from None
        local_kind = cls.__dict__.get("kind") or snake_name(cls.__name__)
        if "." in local_kind:
            raise WorkflowError(
                f"{cls.__name__}.kind {local_kind!r} must be a local name — "
                "the declaring extension supplies the namespace"
            )
        cls.kind = f"{cls.extension}.{local_kind}" if cls.extension else local_kind
        _declare_subject(cls)
        validate_settings_declaration(cls.Settings)
        cls._body_method = _resolve_body_method(cls)
        # Before _wrap_steps: run()'s wrapper signature is (*args, **kwargs).
        cls._run_input_model = _input_model_from_signature(cls)
        if cls._run_input_model:
            claimed = {"account_id"} & set(cls._run_input_model.model_fields)
            if claimed:
                raise WorkflowError(
                    f"{cls.__name__}.{cls._body_method}() declares reserved start() "
                    f"parameter(s) {sorted(claimed)} — attribution is platform "
                    "routing, not workflow input; name the parameter differently"
                )
        _wrap_steps(cls)
        _register_entry(cls)
        workflows.register(cls)

    def __init__(self) -> None:
        self._workflow_id: str = ""
        # What the run is about ({"type", "id"}), set from the dispatch arguments
        # before run(); None for a subjectless framework run.
        self._subject: dict[str, Any] | None = None
        # run()'s validated input bundle (the model synthesized from its signature),
        # set before run() — for templates and derived properties. None = no input.
        self.input: BaseModel | None = None
        # Who requested/triggered the run, replayed off the reserved input
        # key; None on system-owned runs (crons, old checkpoints).
        self.account_id: str | None = None
        self.journal = self.journal_class()
        # The run's warm VM, provisioned lazily and reaped at segment boundaries;
        # its lease expiry decides when it must rotate.
        self._host: Sandbox | None = None

    async def announce(self, topic: str, **facts: Any) -> None:
        # The workflow announcing a domain event in its extension's vocabulary
        # ("pr.opened", pr_number=12, branch="agent/eng-8"). The platform injects
        # the routing subscribers filter on, and the publish runs as its own
        # retrying checkpoint so a recovery replay doesn't re-fire it. Body-only,
        # enforced: the checkpoint is a step, so it can't nest inside one.
        if _in_step.get():
            raise WorkflowError("announce() runs in the workflow body, not inside a @step")

        async def _fan_out() -> None:
            async with step_session():
                await publish(topic, subject=self._subject, kind=self.kind, **facts)

        await DBOS.run_step_async(StepOptions(name=topic, **_IO_RETRIES), _fan_out)

    async def review(
        self, *, questions: list[BaseModel] | None = None, context: str = ""
    ) -> OperatorReply:
        # Park for an in-app decision: the ask carries the controls, any
        # questions, and optional context (rendered beside the reviewed
        # document); the OperatorReply carries the operator's answer (the resume
        # endpoint holds the action to the offered controls). External gates use
        # a Gate. The artifact the reviewer judges isn't named here — a parked
        # run can't produce new ones, so the read side resolves the latest on
        # demand.
        if not self._subject:
            # An in-app review is answered from the subject's surfaces; a
            # subjectless run has none, so nobody would ever see the ask.
            raise SubjectlessGate(OperatorReply.name)
        request = {
            "presentation": "in_app",
            "controls": list(_REVIEW_CONTROLS),
            "questions": [q.model_dump(mode="json") for q in questions or ()],
        }
        if context:
            request["context"] = context
        payload = await _park(self, OperatorReply.name, request, GATE_TTL_SECONDS)
        reply = OperatorReply.model_validate(payload)
        self.journal.add(reply)
        return reply

    async def get_prompt_context(self, **context: Any) -> dict[str, Any]:
        # Everything an agent's template renders with, beyond the workflow and
        # workspace it always gets. Extend via super() to add an extension's standing
        # values (expensive derived prose); the call's own kwargs win on collision.
        return dict(context)

    async def get_workspace_kwargs(self, sandbox: "Sandbox") -> dict[str, Any]:
        # Extend via super() to add the fields workspace_class needs (an extension clones + mints
        # here). Base: just the VM.
        return {"sandbox": sandbox}

    async def get_workspace(self, sandbox: "Sandbox") -> Workspace:
        # What an agent runs in on this run's VM, built per agent call from workspace_class
        # + the extension's kwargs — so short-lived tokens (git) mint fresh each call.
        return self.workspace_class(**await self.get_workspace_kwargs(sandbox))

    async def _ensure_host(self) -> str | None:
        # The warm VM, provisioned once per segment; state is carried in git, so
        # only the host-id matters across steps — held-across-steps never fights replay.
        if not self.steps_reuse_sandbox:
            return
        if self._host and self._host.expires_at:
            remaining = (self._host.expires_at - datetime.now(UTC)).total_seconds()
            if remaining < SANDBOX_HOST_ROTATE_BEFORE_SECONDS:
                # The lease can't cover another worst-case call; rotate to a fresh
                # host. Safe because each call rebuilds its workspace on whatever
                # host it lands on (state lives in git), so a bare VM is fine.
                await self._reap_run()
        if not self._host:
            self._host = await sandbox_client.provision(
                idempotency_key=f"{self._workflow_id}:sandbox"
            )
        return self._host.id

    async def _reap_run(self) -> None:
        if not self._host:
            return
        host, self._host = self._host, None
        await sandbox_client.release(host_id=host.id)

    @property
    def workflow_id(self) -> str:
        return self._workflow_id

    # The effective schedule, resolved like an agent's model/effort/timeout:
    # operator override → the declared default. The reconciler and the settings
    # read both go through these, so the workflow owns its own knobs.
    @classmethod
    def get_schedule(cls) -> str | None:
        return SettingsOverride.workflow_setting(cls.kind, "schedule", cls.every)

    @classmethod
    def has_enabled_schedule(cls) -> bool:
        # There is a schedule and it's on — False for unscheduled workflows too.
        if not cls.every:
            return False
        return SettingsOverride.workflow_setting(cls.kind, "schedule_enabled", True)

    @classmethod
    def settings(cls) -> BaseModel:
        """The workflow's ``Settings``, resolved through the override store — the read
        twin of ``override_setting``, like ``Extension.settings()`` for an extension."""
        values = {
            name: SettingsOverride.workflow_setting(cls.kind, name, field.default)
            for name, field in cls.Settings.model_fields.items()
        }
        return cls.Settings.model_validate(values)

    @classmethod
    def override_setting(cls, field: str, value: Any) -> None:
        # An operator's override for one knob; None clears it back to the declared
        # default. Raises ValueError so the API layer can 422 it. The schedule pair
        # is validated here, not against Settings — those knobs live beside every=.
        if cls.every and field in ("schedule", "schedule_enabled"):
            if field == "schedule" and value is not None and not croniter.is_valid(str(value)):
                raise ValueError(f"Invalid cron expression {value!r}")
            if field == "schedule_enabled" and value is not None and not isinstance(value, bool):
                raise ValueError(f"schedule_enabled must be a bool, got {value!r}")
        elif field not in cls.Settings.model_fields:
            raise ValueError(f"Unknown {cls.kind} setting {field!r}")
        elif value is not None:
            value = coerce_setting_value(cls.Settings, field, value)
            validate_setting_override(cls.Settings, cls.settings().model_dump(), field, value)
        SettingsOverride.set_workflow_setting(cls.kind, field, value)

    @classmethod
    def _validate_subject(cls, subject: "Subject | StoredSubject | None") -> None:
        # The declaration is the contract — subscribers derive their subject class
        # from ``workflow=``, so it has to hold for every run.
        if cls.subject:
            if isinstance(subject, cls.subject):
                return
            given = "nothing" if subject is None else type(subject).__name__
            raise WorkflowError(f"{cls.__name__} is about {cls.subject.__name__}, not {given}")
        if subject is None:
            return
        raise WorkflowError(
            f"{cls.__name__} declares no subject — declare "
            f"``subject = {type(subject).__name__}`` on it, or pass subject=None"
        )

    @classmethod
    async def cancel(cls, subject: Subject | StoredSubject, *, failure: str | None = None) -> None:
        cls._validate_subject(subject)
        runs = Run.list_for_subject(subject.subject_type, str(subject.id), kind=cls.kind)
        run = next((run for run in runs if run.is_active), None)
        if run:
            await run.cancel(failure=failure)

    @classmethod
    async def start(
        cls,
        *,
        subject: Subject | StoredSubject | None,
        account_id: str | None = None,
        **input: Any,
    ) -> str:
        # Mint the id, write the projection row, enqueue the body. Returns the
        # run to follow — a freshly enqueued run, or the subject's already-live
        # one handed back. Enqueuing (not start_workflow) routes execution
        # onto the shared queue, so the process that kicks a run off — often the
        # web process — doesn't have to be the one that runs it; any launched
        # executor picks it up. The input kwargs mirror the body's own signature
        # and validate against the model synthesized from it, so a bad shape
        # fails at start, not inside the run.
        # subject is required (no default) so a run can't silently lose its
        # timeline by omission — pass subject=None explicitly for a background run.
        cls._validate_subject(subject)
        if not account_id:
            # Browser-origin starts inherit the request's authenticated account;
            # dispatchers that know better pass account_id explicitly.
            account_id = current_account_id.get()
        wire: dict[str, Any] = {}
        if cls._run_input_model:
            wire = cls._run_input_model.model_validate(input).model_dump(mode="json")
        elif input:
            raise WorkflowError(f"{cls.__name__}.{cls._body_method}() takes no input")
        if account_id:
            wire[_ACCOUNT_INPUT_KEY] = account_id
        workflow_id = str(uuid7())
        # A subject has at most one active run per workflow kind, enforced by
        # DBOS queue deduplication: the slot is claimed atomically at enqueue,
        # held while the workflow is enqueued or pending (a parked run keeps
        # it), and freed by DBOS itself at the terminal outcome — including
        # when DBOS gives up on a dead workflow. A duplicate start() hands back
        # the live run's id. Subjectless runs are unbounded.
        enqueue_options = (
            SetEnqueueOptions(deduplication_id=f"{cls.kind}:{subject.subject_type}:{subject.id}")
            if subject
            else nullcontext()
        )
        # The workflow's routing metadata, stamped as DBOS custom attributes so
        # "runs for this subject" is answered by workflow_status itself. The
        # subject id is stamped as a string — the one shape every reader compares.
        attributes = None
        if subject:
            attributes = {
                "subject_type": subject.subject_type,
                "subject_id": str(subject.id),
                "subject_label": subject.label,
            }
        subject_record = subject.identity if subject else None
        try:
            with (
                SetWorkflowID(workflow_id),
                SetWorkflowAttributes(attributes),
                enqueue_options,
            ):
                await run_queue.enqueue_async(cls._entry, subject_record, wire)
        except DBOSQueueDeduplicatedError as duplicate:
            holder = _get_dbos_instance()._sys_db.get_deduplicated_workflow(
                run_queue.name, duplicate.deduplication_id
            )
            if holder:
                return holder
            # The holder reached terminal between the rejection and the lookup —
            # the slot is free now, so this start goes through.
            return await cls.start(subject=subject, account_id=account_id, **input)
        # The body also creates its row (idempotently) — this one just makes it
        # visible before an executor picks the workflow up.
        Run.create_row(
            _step_engine(), workflow_id=workflow_id, kind=cls.kind, account_id=account_id
        )
        if subject:
            await publish(WorkflowEvent.SCHEDULED, subject=subject.identity, kind=cls.kind)
        return workflow_id


def _wrap_steps(cls: type[Workflow]) -> None:
    for method_name, method in list(vars(cls).items()):
        if getattr(method, "_durable_step", False):
            name = getattr(method, "_step_name", None) or method_name
            setattr(cls, method_name, _make_step(cls.kind, name, method))


def _make_step(kind: str, name: str, method: Callable) -> Callable:
    # Run the method inside its own session via a zero-arg closure, so `self` is
    # never serialized into the DBOS checkpoint.
    async def _step(self: Workflow, *args: Any, **kwargs: Any) -> Any:
        async def _do() -> Any:
            token = _in_step.set(True)
            try:
                async with step_session():
                    return await method(self, *args, **kwargs)
            finally:
                _in_step.reset(token)

        return await DBOS.run_step_async(StepOptions(name=f"{kind}.{name}"), _do)

    _step.__name__ = name
    _step.__wrapped__ = method  # lets a test call run() without DBOS
    return _step


def _bind_instance(
    cls: type[Workflow],
    subject: dict[str, Any] | None = None,
    input: dict[str, Any] | None = None,
) -> tuple[Workflow, dict[str, Any]]:
    """A workflow instance carrying its subject and validated input, with the
    keyword arguments its body takes."""
    instance = cls()
    instance._subject = subject
    # Platform routing comes off before body validation; an old checkpoint
    # without the key replays account-less.
    input = dict(input or {})
    instance.account_id = input.pop(_ACCOUNT_INPUT_KEY, None)
    # The body's input re-validates from its wire dict; a cron fires with no
    # input, so a scheduled workflow must default every parameter. The validated
    # bundle also lands on the instance for templates / derived properties.
    run_kwargs: dict[str, Any] = {}
    if cls._run_input_model:
        validated = cls._run_input_model.model_validate(input)
        instance.input = validated
        run_kwargs = {name: getattr(validated, name) for name in type(validated).model_fields}
    return instance, run_kwargs


async def _run_instance(
    cls: type[Workflow],
    subject: dict[str, Any] | None = None,
    input: dict[str, Any] | None = None,
) -> Any:
    instance, run_kwargs = _bind_instance(cls, subject, input)
    instance._workflow_id = DBOS.workflow_id  # type: ignore[assignment]
    token = current_workflow.set(instance)
    try:
        return await _execute_run(
            instance._workflow_id,
            cls.kind,
            subject,
            instance.account_id,
            lambda: getattr(instance, cls._body_method)(**run_kwargs),
        )
    finally:
        current_workflow.reset(token)
        await instance._reap_run()


def _register_entry(cls: type[Workflow]) -> None:
    # The closure binds cls outside the durable arguments: the DBOS workflow
    # NAME (the kind) is what says which class this is, so recovery rebinds by
    # name and no class object ever rides a checkpoint.
    @DBOS.workflow(name=cls.kind)
    async def _entry(subject: dict[str, Any] | None, input: dict[str, Any]) -> None:
        await _run_instance(cls, subject, input)

    cls._entry = staticmethod(_entry)  # type: ignore[assignment]

    if cls.every:
        register_schedule(cls, partial(_run_instance, cls))
