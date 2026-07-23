# Writing an extension

An extension is an application installed into Druks as its own Python
distribution. It owns domain behavior; Druks supplies durable execution and
shared operating services. Read [the extension boundary](concepts.md#the-extension-boundary)
before choosing which side should own a capability.

## Scaffold and prove the package

From a Druks checkout:

```bash
uv run druks create extension night_watch
cd druks-night_watch
uv sync
uv run pytest
```

The command writes a standalone `druks-night_watch` project in the current
directory. Its `pyproject.toml` contains:

```toml
[project.entry-points."druks.extensions"]
night_watch = "druks_night_watch.extension:NightWatch"
```

The name must match `[a-z][a-z0-9_]*`. It becomes the API namespace, table
prefix, migration version-table suffix, and settings namespace. Installing the
distribution is the registration:

```bash
uv pip install -e /path/to/druks-night_watch
```

At boot Druks imports installed entry points and fails loudly on duplicate
names, a mismatched entry-point key, malformed target, import failure, or
unprefixed table.

## Package layout

The scaffold separates self-registering capability modules from ordinary
application modules:

| Path | Contract |
| --- | --- |
| `extension.py` | `Extension` subclass, agents, extension settings, subject read-side |
| `workflows.py` | durable `Workflow` and `Gate` subclasses |
| `models.py` | SQLAlchemy models with `<name>_` table names |
| `contracts.py` | `AgentOutput` contracts |
| `schemas.py` | HTTP responses and subject summaries |
| `routes.py` | FastAPI routers |
| `subscribers.py` | signal reactions |
| `webhooks.py` | authenticated provider deliveries; add when needed |
| `migrations/versions/` | this distribution's Alembic history |
| `dist/` | optional standalone frontend served at `/app/<name>` |

Druks recursively discovers leaf modules named `workflows`, `routes`,
`subscribers`, and `webhooks`. A capability hidden in `workflow.py` is not
discovered. Ordinary names such as `policy.py` and `workspace.py` have no import
side effect unless a discovered module imports them.

## Declare the extension

```python
from druks.extensions import Extension


class NightWatch(Extension):
    name = "night_watch"
    icon = "telescope"
    description = "Checks repositories after hours."
```

The class is a stateless install singleton; do not instantiate it. Druks mounts
every router found in its `routes` modules under `/api/night_watch`, supplies
transcript routes, and serves `druks_night_watch/dist/` at
`/app/night_watch` when it contains `index.html`.

## Choose the right workflow shape

The parameters of `run()` or `run_multistep()` are the workflow input. Druks
builds a Pydantic model from their annotations and validates the call to
`start()`.

Use `run()` when the whole body is one durable operation:

```python
from druks.workflows import Workflow


class RecordHeartbeat(Workflow):
    async def run(self, source: str) -> None:
        Heartbeat.record(source)
```

Use `run_multistep()` when completed operations should recover independently or
the workflow waits on a gate:

```python
from druks.workflows import Workflow, step


class Sweep(Workflow):
    async def run_multistep(self, repo: str) -> None:
        findings = await self.scan(repo)
        await NightWatch.report(repo=repo, findings=findings)

    @step
    async def scan(self, repo: str) -> list[str]:
        return await scanner.scan(repo)
```

`run()` is automatically one step and must not carry `@step`.
`run_multistep()` is replayed orchestration and must not itself carry `@step`;
decorate its side-effecting operations instead. An agent called directly from
the orchestration body gets its own step. An agent called inside `@step` or
`run()` shares that enclosing checkpoint.

Completed checkpoints are reused on recovery. An interrupted operation can run
again, so use provider idempotency keys for writes. Keep decisions in replayable
control flow and I/O inside steps. See
[durability and recovery](concepts.md#durability-and-recovery).

Start a workflow with an explicit subject:

```python
run_id = await Sweep.start(
    subject={"type": "repository", "id": repo_id},
    repo=full_name,
)
```

Pass `subject=None` deliberately for background work. A subject has at most one
active run of a workflow kind; a duplicate start returns the active run id —
attribution never changes that (two accounts starting the same subject share
the one run). Wrap `start()` in a domain `dispatch()` method when the extension
needs lookup, snapshot, or routing policy before launch.

A browser-origin start attributes itself: the request identity gate stamps
the resolved account, and `start()` inherits it — a route that starts a
workflow needs no ceremony. Pass `account_id` only when the dispatcher knows
better (a webhook dispatch resolving the ticket assignee). Each agent call
executes with the run's account's own connection, else the install's fallback
account — the charged account is recorded on the call, so a fallback is
visible by comparison. Runs with no account anywhere (crons, background work)
run as the system account. Resuming a parked run keeps its original attribution;
the person clicking Resume never becomes the payer.

### The journal

Druks keeps a journal of each run's typed values: every body-level agent call
and every gate reply lands in it automatically, in call order. Add your own
values with `self.journal.add()`; read them back by contract type:

```python
self.journal.filter(PlanData)                                # all entries, oldest first
self.journal.latest(PlanData)                                # newest, or None
self.journal.filter(ImplementationOutput, status="success")  # keyword filters: ANDed equality
self.journal.filter(ReviewWork)                              # gate replies, by their Gate class
```

Subclass `Journal` to name your projections, and declare it on the workflow:

```python
class SweepJournal(Journal):
    @property
    def findings(self) -> list[FindingData]:
        return self.filter(FindingData)


class Sweep(Workflow):
    journal_class = SweepJournal
```

The journal survives crashes without being stored: recovery re-runs the body
with every durable call memoized, so the same entries land in the same order.

Two rules:

- Only body-level calls are journaled. An agent call inside a `@step` — or in
  a `run()` body, which is one big step — never lands there; keep that state
  in local variables.
- Never mutate body-held state inside a `@step`: a completed step is skipped
  on replay, so the write disappears.

### Schedules and settings

Set `every` to declare a cron:

```python
class Sweep(Workflow):
    every = "0 6 * * *"
```

Every parameter of a scheduled workflow needs a default because the scheduler
supplies no input. Druks evaluates cron expressions in the operator timezone.
The dashboard can retune or disable a declared schedule but cannot invent a new
workflow schedule.

A workflow may declare its own operator settings:

```python
from pydantic import BaseModel, Field


class Sweep(Workflow):
    class Settings(BaseModel):
        batch_size: int = Field(default=20, ge=1, le=100)

    @step
    async def load_settings(self) -> "Sweep.Settings":
        return self.settings()
```

Reading settings inside a step snapshots them for replay. Reading them directly
from replayed orchestration allows later edits to change an in-flight run.

## Add an agent

An agent belongs to the extension class. Its family default (`claude` or
`codex`) resolves through the corresponding operator harness setting; a full
model name pins the default.

```python
from druks.agents import Agent, AgentOutput


class ReportOutput(AgentOutput):
    title: str
    body: str


class NightWatch(Extension):
    report = Agent(
        model="claude",
        prompt="night_watch/report.md",
        contract=ReportOutput,
        description="Turns findings into an operator report.",
    )
```

Call it only inside a workflow:

```python
result = await NightWatch.report(repo=repo, findings=findings)
```

The prompt is rendered with the current workflow, workspace, and supplied
context. The selected harness provisions or attaches a sandbox, executes the
CLI, validates the structured output, and records the call. Override
`AgentOutput.to_result()` to map the strict agent contract to a domain value;
override `get_artifact()` to publish a reviewable artifact.

Do not ask the framework to infer application side effects from agent prose.
The prompt or a subsequent explicit step owns those actions.

## Customize the workspace

Every agent runs through a `Workspace` around a Drukbox sandbox. Override
`Workflow.workspace_class` and `get_workspace_kwargs()` when the application
needs to clone a repository, mint a short-lived token, or require an MCP server.

Keep durable application state outside the VM. A workflow may opt into
`steps_reuse_sandbox = True` to retain one host across a segment, but Druks
releases it at a gate and at workflow exit and rotates it near lease expiry.

Provider selection is an operator concern. Extension workspace code targets the
Druks sandbox contract, not `exe`, AWS, or Docker directly.

## Wait for input

A gate's fields are the reply schema; `name` pins the gate's durable identity —
the recv channel and the parked run's `gate` on the read side (declaring it is
required — the identity must survive a class rename):

```python
from typing import Literal

from druks.workflows import Gate, Workflow


class ApproveReport(Gate):
    name = "approve_report"
    action: Literal["approve", "revise", "cancel"]
    note: str | None = None

    @classmethod
    async def on_wait(cls, workflow: Workflow) -> None:
        await notifier.report_ready(workflow.workflow_id)
```

Wait from `run_multistep()`:

```python
reply = await ApproveReport.wait(
    input_request={
        "presentation": "external",
        "label": "Review the night-watch report",
        "url": review_url,
    }
)
```

`on_wait()` is a checkpointed notification step. The workflow then parks
durably and releases its warm sandbox. The owning external system resumes the
run with `Run.resume()` or a webhook reaction.

For a subject-backed decision inside the Druks dashboard, use:

```python
reply = await self.review(questions=report.questions)
```

It offers `approve`, `request_changes`, and `cancel`. A subjectless workflow
cannot use in-app review. A subjectless custom gate must override `on_wait()` so
the wait is visible; otherwise Druks raises instead of parking silently.

Raise `FatalError` for a deliberate domain stop. Subclass it and set `code`
when readers need a stable machine failure code. Unexpected exceptions fail
the run and are re-raised to DBOS.

## Give runs a subject read-side

Set one subject type and return extension-owned summaries:

```python
from druks.workflows import SubjectSummary


class RepositorySummary(SubjectSummary):
    name: str
    open_findings: int


class NightWatch(Extension):
    subject_type = "repository"

    @classmethod
    def subject_summary(cls, subject_id: str) -> RepositorySummary | None:
        ...

    @classmethod
    def list_subjects(cls) -> list[RepositorySummary]:
        ...
```

Druks mounts a board and per-subject point-in-time and SSE routes under
`/api/night_watch/repository`. It composes each summary with generic run status,
timeline, agent calls, artifacts, and the current gate. Override
`subject_activity()` only for a transient application-specific phase.

## Record events and react to signals

Record an extension event through the extension so ownership is stamped:

```python
NightWatch.record_event(
    type="report.published",
    subject={"type": "repository", "id": repo_id},
    payload={"url": report_url},
)
```

Override `format_event()` to turn extension events into `FeedItem` rows.
Lifecycle events for subjected workflows are recorded automatically. Call
`record_event()` inside a platform-bound transaction such as a request,
durable step, or subscriber.

React with filters rather than body guards:

```python
from druks.signals import subscribe


@subscribe("run.finished", subject__type="repository")
async def on_sweep_finished(*, subject: dict, **_: object) -> None:
    ...
```

Signals are at-least-once. A subscriber exception propagates so webhook
providers or DBOS retry the publication; make reactions idempotent.

## Receive webhooks

A webhook authenticates and normalizes provider input. It should publish a
domain-neutral signal rather than contain application workflow policy:

```python
from fastapi.responses import JSONResponse

from druks.signals import publish
from druks.webhooks import Webhook, verify_hmac_sha256


class NightWatchWebhook(Webhook):
    provider = "night_watch"
    category = "events"

    def request_is_authentic(self) -> bool:
        verify_hmac_sha256(
            self.raw_body,
            self.request.headers.get("x-signature"),
            secret,
        )
        return True

    def get_action(self) -> str:
        return self.data["type"].replace(".", "_")

    async def on_report_approved(self) -> JSONResponse:
        await publish("report.approved", payload=self.data)
        return JSONResponse({"accepted": True})
```

The public path is `/_external/night_watch/events/`. Druks deduplicates a
delivery when the class supplies a delivery key. A failing handler releases the
claim so the provider can retry.

## Models and migrations

Models subclass `druks.db.Base` and every normal extension table starts with
`<name>_`:

```python
from sqlalchemy.orm import Mapped, mapped_column

from druks.db import Base


class Report(Base):
    __tablename__ = "night_watch_reports"

    id: Mapped[int] = mapped_column(primary_key=True)
```

Generate the extension's revision after the model is importable:

```bash
uv run druks makemigrations night_watch -m "add reports"
uv run druks init-db
```

Druks scopes autogeneration to the table prefix and writes the version to
`alembic_version_night_watch`. Query through `druks.db.db_session()` inside an
HTTP request, durable step, or other platform-bound session.

HTTP response models subclass `druks.schemas.BaseResponse`, whose snake_case
fields serialize as camelCase. Request models are ordinary Pydantic models.
Every router declared in a discovered `routes.py` is mounted below the
extension namespace.

## Extension settings and checks

An inner Pydantic `Settings` class defines dashboard-editable knobs:

```python
from typing import Literal

from pydantic import BaseModel, Field, SecretStr


class NightWatch(Extension):
    class Settings(BaseModel):
        severity: Literal["warning", "critical"] = "warning"
        service_token: SecretStr | None = Field(default=None, min_length=8)
```

Supported display shapes are scalar values, `Literal` choices, and
`SecretStr`, including optional forms. Nested Pydantic models are rejected.
Secret values and submitted validation errors are redacted. Read the resolved
model with `NightWatch.settings()`.

An extension may contribute its own `druks doctor` checks through `checks`.
Return `druks.doctor.CheckResult`; Druks namespaces the result and converts a
raising or malformed check into a failure without hiding later checks.

## Frontends

The scaffold ships a minimal `druks_night_watch/dist/index.html`. Replace that
directory with a static frontend build to serve a standalone extension app at
`/app/night_watch`; history fallback and cache headers are handled by FastAPI.

The bundled Druks SPA also has a shared React extension registry. Joining that
shell requires compiling the extension's UI module into the dashboard image;
installing a Python wheel cannot mutate an existing JavaScript bundle. See the
[frontend guide](../frontend/README.md) for that in-repository path.

## Stable author imports

Import from concern namespaces, not from `druks.durable` or internal modules:

| Namespace | Public names |
| --- | --- |
| `druks.extensions` | `Extension` |
| `druks.agents` | `Agent`, `AgentOutput` |
| `druks.workflows` | `Workflow`, `Gate`, `step`, run/agent response types, lifecycle enums and workflow errors |
| `druks.db` | `Base`, `db_session` |
| `druks.schemas` | `BaseResponse` |
| `druks.signals` | `subscribe` |
| `druks.events` | `Event`, `FeedItem` |
| `druks.prompts` | `render_prompt` |
| `druks.webhooks` | `Webhook`, `verify_hmac_sha256` |

The root `druks` package deliberately exports only its version.
