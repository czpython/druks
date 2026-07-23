from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from dbos import DBOS
from sqlalchemy import ForeignKey, Index, String, func, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Mapped, column_property, mapped_column, relationship

from druks.accounts.constants import SYSTEM_ACCOUNT_ID
from druks.accounts.models import Account
from druks.core.models import Uuid7Pk
from druks.database import db_session, get_session
from druks.durable.dbos_state import state_expression, subject_filter, updated_at_expression
from druks.durable.enums import ACTIVE_STATES, AgentCallStatus, RunState
from druks.harnesses.artifacts import normalize_token_usage
from druks.models import Base
from druks.notifications.models import Notification
from druks.settings import load_settings

if TYPE_CHECKING:
    from druks.sandbox.datastructures import AgentResult


class Run(Base):
    __tablename__ = "durable_runs"

    # The DBOS workflow id, minted at start() so row and run share one identity.
    id: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str]
    # The parked gate's recv topic — which gate, e.g. "review_plan"; presence ⇒
    # PENDING_INPUT. The DBOS routing key, set automatically from the Gate class.
    input_gate: Mapped[str | None] = mapped_column(default=None)
    # The structured ask the gate declared at Gate.wait(input_request=…) — the extension's
    # opaque payload, surfaced by the read-side and cleared on resume beside input_gate.
    input_request: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    # When the run last asked for input — a historical fact (input_gate says
    # whether it's still waiting), and the one transition DBOS doesn't stamp.
    input_requested_at: Mapped[datetime | None] = mapped_column(default=None)
    # The receipt: the input_requested_at stamp an answer last resumed.
    answer_parked_at: Mapped[datetime | None] = mapped_column(default=None)
    failure: Mapped[str | None] = mapped_column(default=None)
    # The FatalError subtype's code when the run stopped on a deliberate domain
    # error, so read-sides tell e.g. a gate timeout from a crash without parsing
    # `failure`. Empty/None for a crash.
    failure_code: Mapped[str | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(default=Base.utc_now)
    # Derived, never stored: DBOS owns whether the workflow is live — every
    # SELECT (session.get() on a not-yet-loaded instance included) computes it
    # fresh; an already-loaded instance keeps what it read until expired.
    # Read-only; an operator ends a run through cancel().
    state: Mapped[str] = column_property(state_expression(id, input_gate, created_at))
    # Who asked; the system account when nobody did (crons, background work).
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT"), default=SYSTEM_ACCOUNT_ID
    )
    account: Mapped[Account] = relationship(
        lazy="joined", innerjoin=True, foreign_keys=[account_id]
    )

    # When the run last changed — the newest of creation, the parked ask, and
    # DBOS's status write.
    updated_at: Mapped[datetime] = column_property(
        func.greatest(created_at, input_requested_at, updated_at_expression(id))
    )

    @property
    def is_active(self) -> bool:
        return self.state in {s.value for s in ACTIVE_STATES}

    @property
    def is_parked(self) -> bool:
        return self.state == RunState.PENDING_INPUT.value

    @property
    def is_running(self) -> bool:
        return self.state == RunState.RUNNING.value

    @classmethod
    def create_row(cls, engine, *, workflow_id: str, kind: str, account_id: str | None) -> None:
        # Own committed transaction (not the caller's request txn) so the row
        # exists before the running workflow's first lifecycle event. Idempotent:
        # a scheduled run creates its row inside the (replayable) body, and a
        # start that races its own retry must not double-insert.
        with get_session(engine) as session:
            session.execute(
                pg_insert(cls)
                .values(id=workflow_id, kind=kind, account_id=account_id or SYSTEM_ACCOUNT_ID)
                .on_conflict_do_nothing()
            )
            session.commit()

    @classmethod
    def get(cls, workflow_id: str) -> "Run | None":
        return db_session().get(cls, workflow_id)

    @classmethod
    def list_for_subject(
        cls, subject_type: str, subject_id: str, kind: str | None = None
    ) -> list["Run"]:
        # Every run about this subject (stamped at start), newest first — a
        # subject's lifecycle spans many runs, so its status is theirs
        # aggregated. ``kind`` narrows to one workflow's runs; per (kind,
        # subject) the queue dedup makes runs strictly sequential, so newest
        # first holds within a kind too.
        stmt = (
            select(cls)
            .where(subject_filter(cls.id, subject_type, subject_id))
            .order_by(cls.updated_at.desc())
        )
        if kind:
            stmt = stmt.where(cls.kind == kind)
        return list(db_session().scalars(stmt))

    def get_ask(self) -> dict[str, Any]:
        # The parked ask, ready to serve. An in-app review's ask names neither
        # label nor artifact — a parked run can't produce new artifacts, so the
        # latest resolves here on demand; fields the gate declared win.
        ask = self.input_request
        if not ask:
            raise ValueError(f"run {self.id} is not parked on an ask")
        if ask.get("presentation") != "in_app":
            return ask
        artifact = Artifact.get_latest_for_run(self.id)
        return {
            "label": f"Review: {artifact.title}" if artifact else "Review",
            "artifact_id": artifact.id if artifact else None,
            **ask,
        }

    def get_rendered_ask(self) -> dict[str, Any]:
        # The parked ask as notification content. The body is the ask's own
        # prose (plain text, never a template); actions are data — deliver()
        # renders the buttons and encodes the token. Shape-dispatch on
        # presentation happens before any subscripting, so the external branch
        # never touches in-app-only keys.
        ask = self.get_ask()
        if ask["presentation"] == "in_app":
            questions = ask["questions"]
            lines = [ask["label"], *(question["prompt"] for question in questions)]
            actions = [
                {"id": control, "label": control.replace("_", " ").capitalize()}
                for control in ask["controls"]
            ]
            actions += [
                {"id": option["id"], "label": option["label"]}
                for question in questions
                for option in question["options"]
            ]
            return {"body": "\n".join(lines), "actions": actions, "deep_link": None}
        # External asks are informational — answered on their source, so no
        # actions; url is an optional gate-author-declared view-link.
        return {"body": ask["label"], "actions": None, "deep_link": ask.get("url")}

    def create_park_notification(self, destination_id: str, subject: dict[str, Any]) -> str:
        # Create the notification for the round this run just parked on — the
        # caller supplies the run's subject and enqueues delivery. run_id +
        # run_parked_at snapshot the round so a click on an old button can be
        # refused once the run re-parks.
        rendered = self.get_rendered_ask()
        notification = Notification.create(
            destination_id=destination_id,
            reason="gate.parked",
            body=rendered["body"],
            subject=subject,
            actions=rendered["actions"],
            run_id=self.id,
            run_parked_at=self.input_requested_at,
            deep_link=rendered["deep_link"],
        )
        return notification.id

    async def resume(self, **fields: Any) -> None:
        # Answer the gate this run is parked on: send the reply to its recv topic
        # so the workflow's Gate.wait() wakes and validates it. The run binds
        # (id, gate), so a resumer needs neither — and can't target a gate the run
        # isn't on; a None topic would silently send into the void.
        if not self.input_gate:
            raise ValueError(f"run {self.id} is not parked on a gate")
        # The idempotency key names the parked round the resumer read (each park
        # stamps a fresh input_requested_at beside the gate). One round admits one
        # reply: DBOS keeps a consumed notification under its key, so a duplicate —
        # concurrent or late — collapses against it instead of buffering FIFO on
        # the topic and ghost-resuming the gate's next round with a stale answer.
        await DBOS.send_async(
            self.id,
            fields,
            topic=self.input_gate,
            idempotency_key=f"{self.input_gate}:{self.input_requested_at}",
        )

    async def cancel(self, *, failure: str | None = None) -> None:
        # Clear the ask (so nothing tries to answer it) and keep the operator's
        # reason, then cancel the DBOS workflow — that writes the CANCELLED
        # status state derives from, dequeues it, and frees the subject's dedup
        # slot, so a new run can start now rather than at GATE_TTL. The session
        # never wrote state, so a Run loaded before this call still carries the
        # old one — expire or re-select before serializing it.
        self.input_gate = None
        self.input_request = None
        self.failure = failure
        db_session().flush()
        await DBOS.cancel_workflow_async(self.id)


@dataclass(frozen=True)
class RunArtifactLayout:
    """Where an AgentCall's bytes live on disk, by role. ``transcript`` is the
    rich agent rollout the UI shows (the stdout stream); ``stderr`` is the
    process stderr; ``output`` is the final structured output; ``prompt`` /
    ``metadata`` are the rendered prompt and run metadata; ``manifest`` is the
    capability manifest — what the call was delivered or skipped. Each path is
    ``call_dir / <fixed name>`` — always known; the file may or may not exist
    yet."""

    transcript: Path
    stderr: Path
    output: Path
    prompt: Path
    metadata: Path
    manifest: Path


class AgentCall(Base, Uuid7Pk):
    __tablename__ = "agent_calls"
    __table_args__ = (
        Index("agent_calls_run_idx", "run_id"),
        Index("agent_calls_account_finished_idx", "account_id", "finished_at"),
    )

    # Which model ran this row, snapshotted at dispatch; cost analysis and the
    # transcript layout both read it. Nullable — a call may be recorded before
    # its model is resolved.
    model: Mapped[str | None] = mapped_column(String, default=None)
    # The run this LLM call ran in. ON DELETE CASCADE, so a call never outlives it.
    run_id: Mapped[str] = mapped_column(ForeignKey("durable_runs.id", ondelete="CASCADE"))
    run: Mapped["Run"] = relationship()
    # Which agent (registry id: "scope", "implement", …) made this call — the
    # timeline's grouping label. Nullable — not every call is agent-attributed.
    agent: Mapped[str | None] = mapped_column(String, default=None)
    # The subscription actually charged — differs from the run's account on
    # fallback.
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT"), default=SYSTEM_ACCOUNT_ID
    )
    account: Mapped[Account] = relationship(lazy="joined", innerjoin=True)

    created_at: Mapped[datetime] = mapped_column(default=Base.utc_now)
    started_at: Mapped[datetime] = mapped_column(default=Base.utc_now)
    status: Mapped[str] = mapped_column(default=AgentCallStatus.RUNNING.value)
    finished_at: Mapped[datetime | None]
    last_error: Mapped[str | None]
    cost_usd: Mapped[float | None]
    cost_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    # The VM this run executed on. Resolve via the sandbox service when needed
    # and treat 404 as the host being gone.
    sandbox_host_id: Mapped[str]

    def get_live_status(self) -> str:
        # Unfinished: "running" while the run is live, "abandoned" once it's terminal.
        if self.finished_at:
            return AgentCallStatus(self.status).value
        if self.run.is_active:
            return "running"
        return "abandoned"

    @property
    def artifact_dir(self) -> str:
        return str(load_settings().artifacts_dir / f"run-{self.run_id}")

    @property
    def call_dir(self) -> Path:
        return Path(self.artifact_dir) / self.id

    @property
    def artifact_layout(self) -> RunArtifactLayout:
        # The sandbox runner streams every run's stdout to stdout.jsonl no matter
        # which harness runs it, so the layout never needs the model (which may
        # still be unresolved on this row).
        sub = self.call_dir
        return RunArtifactLayout(
            transcript=sub / "stdout.jsonl",
            stderr=sub / "stderr.log",
            output=sub / "output.json",
            prompt=sub / "prompt.md",
            metadata=sub / "metadata.json",
            manifest=sub / "manifest.json",
        )

    def get_file_path(self, name: str) -> Path | None:
        # Safe-resolve a download path inside this call's dir; block traversal.
        candidate = (self.call_dir / name).resolve()
        try:
            candidate.relative_to(self.call_dir.resolve())
        except ValueError:
            return
        return candidate if candidate.is_file() else None

    def get_stream_path(self, stream: Literal["stdout", "stderr"]) -> Path | None:
        layout = self.artifact_layout
        candidate = layout.transcript if stream == "stdout" else layout.stderr
        return candidate if candidate.exists() else None

    @classmethod
    def start(
        cls,
        engine,
        *,
        call_id: str,
        run_id: str,
        model: str | None,
        agent: str | None,
        host_id: str,
        account_id: str,
    ) -> None:
        # Recorded RUNNING once the agent starts on its host (id = its on-disk
        # transcript dir) in its own committed transaction, so the live step
        # shows while the agent works — the running step's session won't commit
        # until it ends. Provisioning isn't part of the call, so the row only
        # exists once there's a host to run on.
        with get_session(engine) as session:
            # A crash-recovered step re-runs with a fresh call id; abandon the
            # prior attempt's RUNNING row (a run's calls are sequential) so it
            # doesn't linger as a phantom live step.
            session.execute(
                update(cls)
                .where(cls.run_id == run_id, cls.status == AgentCallStatus.RUNNING.value)
                .values(status=AgentCallStatus.ABANDONED.value, finished_at=Base.utc_now())
            )
            session.add(
                cls(
                    id=call_id,
                    run_id=run_id,
                    agent=agent,
                    model=model,
                    sandbox_host_id=host_id,
                    account_id=account_id,
                )
            )
            session.commit()

    @classmethod
    def finish(cls, engine, *, call_id: str, result: "AgentResult") -> None:
        with get_session(engine) as session:
            call = session.get(cls, call_id)
            call.status = result.status.value
            call.started_at = result.started_at
            call.finished_at = Base.utc_now()
            call.last_error = result.last_error
            call.cost_usd = result.cost_usd
            call.cost_metadata = result.cost_metadata
            session.commit()

    @classmethod
    def fail(cls, engine, *, call_id: str, error: str) -> None:
        # The run raised after the call started (a cancel, or a crash past the
        # agent body) — close the row so it doesn't linger as a phantom step.
        with get_session(engine) as session:
            call = session.get(cls, call_id)
            call.status = AgentCallStatus.FAILED.value
            call.finished_at = Base.utc_now()
            call.last_error = error
            session.commit()

    @classmethod
    def get(cls, agent_call_id: str) -> "AgentCall | None":
        return db_session().get(cls, agent_call_id)

    @classmethod
    def list_for_run(cls, run_id: str) -> list["AgentCall"]:
        # Execution order, so the read side zips calls onto their runs.
        stmt = select(cls).where(cls.run_id == run_id).order_by(cls.created_at, cls.id)
        return list(db_session().scalars(stmt))

    @classmethod
    def by_run(cls, run_ids: list[str]) -> dict[str, list["AgentCall"]]:
        # The calls under each run, in execution order — one query for a timeline.
        stmt = select(cls).where(cls.run_id.in_(run_ids)).order_by(cls.created_at, cls.id)
        grouped: dict[str, list[AgentCall]] = {run_id: [] for run_id in run_ids}
        for call in db_session().scalars(stmt):
            grouped[call.run_id].append(call)
        return grouped

    @classmethod
    def list_for_subject(cls, subject_type: str, subject_id: str) -> list["AgentCall"]:
        stmt = (
            select(cls)
            .where(subject_filter(cls.run_id, subject_type, subject_id))
            .order_by(cls.created_at, cls.id)
        )
        return list(db_session().scalars(stmt))

    @classmethod
    def total_run_spend_between(cls, *, start: datetime, end: datetime) -> tuple[float, int]:
        stmt = (
            select(cls.cost_usd, cls.cost_metadata)
            .where(cls.finished_at.is_not(None))
            .where(cls.finished_at >= start)
            .where(cls.finished_at < end)
        )
        cost = 0.0
        tokens = 0
        for cost_usd, metadata in db_session().execute(stmt):
            if cost_usd is not None:
                cost += float(cost_usd)
            canonical = normalize_token_usage(metadata)
            if canonical:
                tokens += canonical["total_tokens"]
        return cost, tokens

    def record_cost(self, *, cost_usd: float | None, cost_metadata: dict | None) -> None:
        if cost_usd is None and not cost_metadata:
            return
        if cost_usd is not None:
            self.cost_usd = cost_usd
        if cost_metadata:
            self.cost_metadata = cost_metadata
        db_session().flush()


class Artifact(Base, Uuid7Pk):
    __tablename__ = "agent_call_artifacts"

    # An artifact is produced by exactly one call and reached only through it —
    # never a timeline query root. Cascade so it never outlives its call.
    agent_call_id: Mapped[str] = mapped_column(
        ForeignKey("agent_calls.id", ondelete="CASCADE"), unique=True
    )
    kind: Mapped[str]
    title: Mapped[str]
    # Content lives in the call dir; the row points at the file by name.
    path: Mapped[str]

    @classmethod
    def record(cls, *, call_dir: Path, call_id: str, kind: str, title: str, content: str) -> None:
        # Platform-owned: write a call's declared renderable output into its dir and
        # record the descriptor on the call's step session. Idempotent per call
        # (unique fk) so a replayed step never double-records.
        name = f"artifact.{'md' if kind == 'markdown' else 'txt'}"
        call_dir.mkdir(parents=True, exist_ok=True)
        (call_dir / name).write_text(content)
        session = db_session()
        session.execute(
            pg_insert(cls)
            .values(agent_call_id=call_id, kind=kind, title=title, path=name)
            .on_conflict_do_nothing(index_elements=["agent_call_id"])
        )
        session.flush()

    @classmethod
    def get_for_call(cls, call_id: str) -> "Artifact | None":
        return db_session().scalar(select(cls).where(cls.agent_call_id == call_id))

    @classmethod
    def get_latest_for_run(cls, run_id: str) -> "Artifact | None":
        # The run's most recent renderable output, reached through its calls — an
        # in-app review shows this beside its controls. Newest call wins.
        return db_session().scalar(
            select(cls)
            .join(AgentCall, AgentCall.id == cls.agent_call_id)
            .where(AgentCall.run_id == run_id)
            # uuid7 ids are time-ordered, so they break a created_at tie toward the
            # call recorded last.
            .order_by(AgentCall.created_at.desc(), AgentCall.id.desc())
            .limit(1)
        )
