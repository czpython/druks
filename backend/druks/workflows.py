import inspect
import re
from collections.abc import Callable
from contextlib import nullcontext
from contextvars import ContextVar
from datetime import UTC, datetime
from functools import partial
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, ClassVar, Self, get_type_hints

from croniter import croniter
from dbos import DBOS, SetEnqueueOptions, SetWorkflowAttributes, SetWorkflowID, StepOptions
from dbos._dbos import _get_dbos_instance
from dbos._error import (
    DBOSAwaitedWorkflowCancelledError,
    DBOSQueueDeduplicatedError,
    DBOSWorkflowCancelledError,
)
from pydantic import BaseModel, ConfigDict, Field, create_model
from uuid_utils import uuid7

from druks.durable.activity import get_run_phase, set_run_phase
from druks.durable.engine import _step_engine, register_schedule, run_queue, step_session
from druks.durable.enums import AgentCallStatus, RunState
from druks.durable.exceptions import FatalError, GateTimeout, SubjectlessGate, WorkflowError
from druks.durable.models import AgentCall, Run
from druks.durable.schemas import AgentCallResponse, SubjectActivity, SubjectSummary
from druks.events.models import Event
from druks.extensions.loader import resolve_workflow_extension
from druks.extensions.registry import workflows
from druks.extensions.settings import (
    coerce_setting_value,
    validate_setting_override,
    validate_settings_declaration,
)
from druks.notifications.outbox import notifications_queue, send_notification
from druks.sandbox.client import sandbox_client
from druks.sandbox.constants import SANDBOX_HOST_ROTATE_BEFORE_SECONDS
from druks.sandbox.datastructures import Workspace
from druks.signals import publish
from druks.user_settings.models import SettingsOverride, UserSettings

# druks.workflows is the author door for workflow authoring: the bases (Workflow,
# Gate, step) defined below plus the run/timeline records re-exported from the
# engine. The engine itself (druks.durable) stays internal.
__all__ = [
    "AgentCall",
    "AgentCallResponse",
    "AgentCallStatus",
    "FatalError",
    "Gate",
    "Run",
    "RunState",
    "SubjectActivity",
    "SubjectSummary",
    "Workflow",
    "WorkflowError",
    "get_run_phase",
    "set_run_phase",
    "step",
]

if TYPE_CHECKING:
    from druks.sandbox.host import Sandbox

# A human gate can park for days; a long recv TTL still caps zombie parks.
GATE_TTL_SECONDS = 14 * 24 * 60 * 60

# The controls an in-app review always offers. Framework-owned: an extension's agent
# supplies the plan and questions (content), but the decision verbs are ours — so a
# resume can only carry an action we defined, the line the resume endpoint checks.
_REVIEW_CONTROLS = ("approve", "request_changes", "cancel")
# The recv topic every in-app review parks on. A run parks on one gate at a time, so
# one topic serves them all; Run.resume routes the reply back through it.
_REVIEW_TOPIC = "review"

# The running workflow instance, so a Gate's on_wait() can reach its extension's
# side-effects (set draft, request review, …) when the gate parks. No default:
# Gate.wait() only runs inside a workflow, so it's always set there.
current_workflow: ContextVar["Workflow"] = ContextVar("current_workflow")
# True while a @step body runs. An agent run inside one is already memoized by that
# step, so it skips wrapping itself; outside, it wraps itself in its own step.
_in_step: ContextVar[bool] = ContextVar("_in_step", default=False)

# Attribution rides the durable input dict beside the body's own kwargs under
# reserved keys (a body param can't start with an underscore), stripped before
# body-model validation — old checkpointed inputs without them replay as-is.
_ACCOUNT_KEY = "_account_id"
_UNATTRIBUTED_KEY = "_unattributed_reason"


class _Subject(BaseModel):
    # What a run is about — the opaque {type, id} the platform keys events to.
    # Modelled so start() validates the shape declaratively instead of by hand.
    model_config = ConfigDict(extra="forbid")
    type: str = Field(min_length=1)
    id: int | str


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
        return None
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


_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def _kind_from_class_name(name: str) -> str:
    return _CAMEL_BOUNDARY.sub("_", name).lower()


class Gate(BaseModel):
    """A typed human-in-the-loop gate. Subclass per park point; the class name is
    the durable recv topic, the fields are the reply's schema. `wait()` parks the
    running workflow until `Run.resume()` answers it (or the TTL lapses) and
    returns the validated reply — both ends of the channel hang off the class."""

    topic: ClassVar[str] = ""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if not cls.__dict__.get("topic"):
            cls.topic = _kind_from_class_name(cls.__name__)

    @classmethod
    async def on_wait(cls, workflow: "Workflow") -> None:
        # Runs when the gate parks, before the run suspends — override to make a
        # human aware there's something to answer (set PR draft, request review,
        # notify Slack, …). Default: nothing.
        return

    @classmethod
    async def wait(
        cls, *, input_request: dict[str, Any] | None = None, ttl_seconds: float = GATE_TTL_SECONDS
    ) -> Self:
        # Suspend the running workflow until its gate is answered. A gate is a
        # run-level state — the read surfaces "needs you" straight off the parked run.
        # ``input_request`` is the plain-dict ask (at least a ``label`` and
        # ``presentation``), stored on the run beside the gate topic and cleared on
        # resume — so an extension declares the ask here, beside on_wait, not at read time.
        workflow = current_workflow.get()
        if not workflow.subject and cls.on_wait.__func__ is Gate.on_wait.__func__:
            # No subject means no feed surface; if on_wait wasn't overridden
            # either, nobody would ever see this park — fail loudly instead
            # of parking invisibly for the whole TTL.
            raise SubjectlessGate(cls.topic)

        async def _on_wait() -> None:
            # on_wait is real IO (it tells a human), so it gets its own checkpointed
            # step with a DB session.
            async with step_session():
                await cls.on_wait(workflow)

        await DBOS.run_step_async(StepOptions(name=f"{cls.topic}._on_wait"), _on_wait)
        payload = await _park(workflow, cls.topic, input_request, ttl_seconds)
        return cls.model_validate(payload)


async def _park(
    workflow: "Workflow",
    topic: str,
    input_request: dict[str, Any] | None,
    ttl_seconds: float,
) -> dict[str, Any]:
    # Shared park core: a park lasts days, so reap the warm VM, then suspend on the topic
    # until Run.resume answers it.
    await workflow._reap_run()
    await _emit_run_event(
        workflow.workflow_id,
        RunState.PENDING_INPUT,
        subject=workflow.subject,
        facts={
            "input_gate": topic,
            "input_request": input_request,
            "input_requested_at": datetime.now(UTC),
        },
    )
    if workflow.subject:
        # Every subjected park notifies the designated destination — no author opt-in.
        await _notify_designated_destination(workflow.workflow_id, workflow.subject)
    payload = await DBOS.recv_async(topic, timeout_seconds=ttl_seconds)
    if payload is None:
        raise GateTimeout(topic)
    await _emit_run_event(
        workflow.workflow_id,
        RunState.RUNNING,
        subject=workflow.subject,
        facts=_GATE_CLEARED,
    )
    return payload


async def _notify_designated_destination(workflow_id: str, subject: dict[str, Any]) -> None:
    # Reads the ask off the run row the pending_input step just wrote (the
    # signal payload carries no ask — producer-side placement is the point);
    # the settings pointer is the operator's off-switch.
    async def _create() -> str | None:
        async with step_session():
            destination_id = UserSettings.get().gate_park_destination_id
            if not destination_id:
                return None
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


# Lifecycle steps do real IO (DB, Redis, subscribers reaching trackers), so a
# transient failure must not become a failed run — a lost reaction has no
# redelivery, unlike a webhook.
_IO_RETRIES: StepOptions = {"retries_allowed": True, "max_attempts": 5}


async def _emit_run_event(
    workflow_id: str,
    event: RunState,
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
            if not subject:
                # Subjectless framework crons are plumbing: no feed entry.
                return None
            return {
                "kind": run.kind,
                "subject": subject,
                "payload": _log_run_event(run, event, subject, result),
            }

    transition = await DBOS.run_step_async(
        StepOptions(name=f"run.{event.value}", **_IO_RETRIES), _transition
    )
    if not transition:
        return

    async def _propagate() -> None:
        async with step_session():
            await publish(
                f"run.{event.value}",
                subject=transition["subject"],
                kind=transition["kind"],
                **{k: v for k, v in transition["payload"].items() if k != "kind"},
            )

    await DBOS.run_step_async(
        StepOptions(name=f"run.{event.value}:propagate", **_IO_RETRIES), _propagate
    )


def _log_run_event(
    run: Run,
    event: RunState,
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
        type=f"run.{event.value}",
        subject=subject,
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
    body: Callable,
) -> Any:
    # Ensure the row (idempotent, so a scheduled run with no start() makes it
    # here), then run the body between its running and finished/failed events.
    # Every failure re-raises so DBOS records the terminal ERROR derived state
    # reads; an operator cancel already carries its own reason and terminal
    # status, so it passes through untouched.
    Run.create_row(_step_engine(), workflow_id=workflow_id, kind=kind)
    await _emit_run_event(workflow_id, RunState.RUNNING, subject=subject)
    try:
        result = await body()
    except (DBOSAwaitedWorkflowCancelledError, DBOSWorkflowCancelledError):
        raise
    except Exception as exc:
        await _emit_run_event(
            workflow_id,
            RunState.FAILED,
            subject=subject,
            facts={
                **_GATE_CLEARED,
                "failure": str(exc),
                "failure_code": exc.code if isinstance(exc, FatalError) else "",
            },
        )
        raise
    await _emit_run_event(workflow_id, RunState.FINISHED, subject=subject, result=result)
    return result


class Workflow:
    kind: ClassVar[str] = ""
    # The extension that declares this workflow — class identity, resolved from
    # the loader's package registrations at definition time and namespacing
    # ``kind``. Never supplied or stored per run.
    extension: ClassVar[str | None] = None
    # When set to a cron string, the workflow also registers a schedule that
    # fires its run() on that cadence (no subject — a framework cron).
    every: ClassVar[str | None] = None
    # True holds one warm VM across the run's agent calls (released at gate parks);
    # False gives each call a throwaway VM.
    steps_reuse_sandbox: ClassVar[bool] = False
    # The Workspace subclass agents run in; an extension sets it (default: the bare VM).
    workspace_class: ClassVar[type[Workspace]] = Workspace
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
        local_kind = cls.__dict__.get("kind") or _kind_from_class_name(cls.__name__)
        if "." in local_kind:
            raise WorkflowError(
                f"{cls.__name__}.kind {local_kind!r} must be a local name — "
                "the declaring extension supplies the namespace"
            )
        cls.kind = f"{cls.extension}.{local_kind}" if cls.extension else local_kind
        validate_settings_declaration(cls.Settings)
        cls._body_method = _resolve_body_method(cls)
        # Before _wrap_steps: run()'s wrapper signature is (*args, **kwargs).
        cls._run_input_model = _input_model_from_signature(cls)
        _wrap_steps(cls)
        _register_entry(cls)
        workflows.register(cls)

    def __init__(self) -> None:
        self._workflow_id: str = ""
        # What the run is about ({"type", "id"}), set from the dispatch arguments
        # before run(); None for a subjectless framework run. Dispatch-only —
        # read, don't write.
        self.subject: dict[str, Any] | None = None
        # run()'s validated input bundle (the model synthesized from its signature),
        # set before run() — for templates and derived properties. None = no input.
        self.input: BaseModel | None = None
        # The account the run was started for (session, ticket assignee) — set
        # from the reserved input key before run(); None with the dispatcher's
        # reason when no account resolved. Resume never rewrites these: they
        # ride the start-time input, so the resumer is never charged.
        self.account_id: str | None = None
        self.unattributed_reason: str | None = None
        # Facts published with set_state, kept warm for sync reads (templates,
        # workspace kwargs); the durable copy is the run's DBOS events.
        self._state_facts: dict[str, Any] = {}
        # The run's warm VM, provisioned lazily and reaped at segment boundaries;
        # its lease expiry decides when it must rotate.
        self._host: Sandbox | None = None

    @property
    def state(self) -> Any:
        # The facts the run published with set_state — nothing else. Input stays
        # on self.input; a reader names which one it means. Durable by
        # determinism: a recovery re-runs the body, whose set_state calls
        # rebuild this in-memory copy (the event writes themselves replay as
        # memoized steps).
        return SimpleNamespace(**self._state_facts)

    async def set_state(self, **facts: Any) -> None:
        # Durably publish facts the run learned mid-flight (a provisioned PR
        # number, a resolved branch). Body-only, enforced: inside a @step the
        # in-memory update would vanish on replay (the step is skipped), which
        # is exactly the state loss this call exists to prevent. Each fact is a
        # DBOS workflow event (memoized — written once); the ``run.state``
        # signal fans out in its own checkpointed step so reactions fire once,
        # inside a session.
        if _in_step.get():
            raise WorkflowError("set_state() runs in the workflow body, not inside a @step")
        for key, value in facts.items():
            await DBOS.set_event_async(key, value)
        self._state_facts.update(facts)

        async def _fan_out() -> None:
            async with step_session():
                await publish("run.state", subject=self.subject, kind=self.kind, **facts)

        await DBOS.run_step_async(StepOptions(name="run.state", **_IO_RETRIES), _fan_out)

    async def review(self, *, questions: list[BaseModel] | None = None) -> dict[str, Any]:
        # Park for an in-app decision: the ask carries the controls and any questions;
        # the reply is {action, answers, note} — an answer is an offered option id or
        # the operator's own words, note their free-text remark. Both are content for
        # the next agent prompt, never control flow (the resume endpoint holds the
        # action to the offered controls). External gates use a Gate. The artifact the
        # reviewer judges isn't named here — a parked run can't produce new ones, so
        # the read side resolves the latest on demand.
        if not self.subject:
            # An in-app review is answered from the subject's surfaces; a
            # subjectless run has none, so nobody would ever see the ask.
            raise SubjectlessGate(_REVIEW_TOPIC)
        request = {
            "presentation": "in_app",
            "controls": list(_REVIEW_CONTROLS),
            "questions": [q.model_dump(mode="json") for q in questions or ()],
        }
        return await _park(self, _REVIEW_TOPIC, request, GATE_TTL_SECONDS)

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
            return None
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
    async def start(
        cls,
        *,
        subject: dict[str, Any] | None,
        account_id: str | None = None,
        unattributed_reason: str | None = None,
        **input: Any,
    ) -> str:
        # Mint the id, write the projection row, enqueue the body. Returns the
        # workflow id; an extension that wants one-active-run-per-subject enforces
        # that on its own side before calling this. Enqueuing (not start_workflow)
        # routes execution onto the shared queue, so the process that kicks a run
        # off — often the web process — doesn't have to be the one that runs it;
        # any launched executor picks it up. The input kwargs mirror the body's own
        # signature and validate against the model synthesized from it, so a bad
        # shape fails at start, not inside the run.
        # subject is required (no default) so a run can't silently lose its
        # timeline by omission — pass subject=None explicitly for a background run.
        if subject is not None:
            _Subject.model_validate(subject)  # raises on a bad shape (wrong/extra keys, types)
        if account_id and unattributed_reason:
            raise WorkflowError("unattributed_reason describes a run WITHOUT an account_id")
        if cls._run_input_model is None:
            if input:
                raise WorkflowError(f"{cls.__name__}.{cls._body_method}() takes no input")
            wire: dict[str, Any] = {}
        else:
            wire = cls._run_input_model.model_validate(input).model_dump(mode="json")
        # Attribution rides reserved keys beside the body's kwargs — stripped
        # again before body validation, so old checkpointed inputs replay as-is.
        if account_id:
            wire[_ACCOUNT_KEY] = account_id
        if unattributed_reason:
            wire[_UNATTRIBUTED_KEY] = unattributed_reason
        workflow_id = str(uuid7())
        # A subject has at most one active run per workflow kind, enforced by
        # DBOS queue deduplication: the slot is claimed atomically at enqueue,
        # held while the workflow is enqueued or pending (a parked run keeps
        # it), and freed by DBOS itself at the terminal outcome — including
        # when DBOS gives up on a dead workflow. A duplicate start() hands back
        # the live run's id. Subjectless runs are unbounded.
        enqueue_options = (
            SetEnqueueOptions(deduplication_id=f"{cls.kind}:{subject['type']}:{subject['id']}")
            if subject
            else nullcontext()
        )
        # The workflow's routing metadata, stamped as DBOS custom attributes so
        # "runs for this subject" is answered by workflow_status itself. The
        # subject id is stamped as a string — the one shape every reader compares.
        attributes = {}
        if subject:
            attributes = {"subject_type": subject["type"], "subject_id": str(subject["id"])}
        if account_id:
            attributes["account_id"] = account_id
        try:
            with (
                SetWorkflowID(workflow_id),
                SetWorkflowAttributes(attributes or None),
                enqueue_options,
            ):
                await run_queue.enqueue_async(cls._entry, subject, wire)
        except DBOSQueueDeduplicatedError as duplicate:
            holder = _get_dbos_instance()._sys_db.get_deduplicated_workflow(
                run_queue.name, duplicate.deduplication_id
            )
            if holder:
                return holder
            # The holder reached terminal between the rejection and the lookup —
            # the slot is free now, so this start goes through.
            return await cls.start(
                subject=subject,
                account_id=account_id,
                unattributed_reason=unattributed_reason,
                **input,
            )
        # The body also creates its row (idempotently) — this one just makes it
        # visible before an executor picks the workflow up.
        Run.create_row(_step_engine(), workflow_id=workflow_id, kind=cls.kind)
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


async def _run_instance(
    cls: type[Workflow],
    subject: dict[str, Any] | None = None,
    input: dict[str, Any] | None = None,
) -> Any:
    instance = cls()
    instance._workflow_id = DBOS.workflow_id  # type: ignore[assignment]
    instance.subject = subject
    input = dict(input or {})
    instance.account_id = input.pop(_ACCOUNT_KEY, None)
    instance.unattributed_reason = input.pop(_UNATTRIBUTED_KEY, None)
    # The body's input re-validates from its wire dict; a cron fires with no
    # input, so a scheduled workflow must default every parameter. The validated
    # bundle also lands on the instance for templates / derived properties.
    run_kwargs: dict[str, Any] = {}
    if cls._run_input_model:
        validated = cls._run_input_model.model_validate(input)
        instance.input = validated
        run_kwargs = {name: getattr(validated, name) for name in type(validated).model_fields}
    token = current_workflow.set(instance)
    try:
        return await _execute_run(
            instance._workflow_id,
            cls.kind,
            subject,
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
