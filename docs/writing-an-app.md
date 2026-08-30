---
title: "Writing an app"
description: "Build a separately packaged Druks app with workflows, agents, gates, routes, models, and migrations."
sidebarTitle: "Author guide"
icon: "puzzle"
---

An app is a Python distribution installed in Druks. It owns domain behavior.
Druks supplies durable execution and shared operating services. Read
[the app boundary](concepts.md#the-app-boundary)
before you assign ownership of a capability.

## Scaffold and prove the package

```bash
uvx --from druks druks create app night_watch
cd druks-night_watch
uv sync
uv run pytest
```

From a Druks checkout, `uv run druks create app night_watch` scaffolds with
that checkout's CLI instead.

The command writes a standalone `druks-night_watch` project in the current
directory. Its `pyproject.toml` contains:

```toml
[project.entry-points."druks.apps"]
night_watch = "druks_night_watch.app:NightWatch"
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

The project root also carries an `AGENTS.md` holding the contracts a coding agent
cannot infer from the stubs, and a link back to this guide.

The scaffold depends on the published `druks`. To develop an app against a
local checkout instead, pin it:

```toml
[tool.uv.sources]
druks = { path = "../druks", editable = true }
```

## Package layout

The scaffold separates self-registering capability modules from ordinary
package modules:

| Path | Contract |
| --- | --- |
| `app.py` | `App` subclass, agents, app settings |
| `workflows.py` | durable `Workflow` and `Gate` subclasses |
| `tasks.py` | Optional `@task` background functions |
| `models.py` | SQLAlchemy models with `<name>_` table names, `StoredSubject` among them |
| `contracts.py` | `AgentOutput` contracts |
| `schemas.py` | HTTP responses and subject summaries |
| `routes.py` | FastAPI routers |
| `pages.py` | `@page` declarations that return `Page` objects |
| `subscribers.py` | signal reactions |
| `webhooks.py` | Optional authenticated provider deliveries |
| `services.py` | Optional `Service` declarations for appliance credentials at external providers |
| `migrations/versions/` | this distribution's Alembic history |
| `dist/` | optional built frontend module, mounted inside the shell (served under `/app/<name>`) |

Druks recursively discovers leaf modules named `workflows`, `tasks`, `routes`,
`pages`, `subscribers`, `webhooks`, and `services`. A capability hidden in
`workflow.py` is not discovered. Ordinary names such as `policy.py` and `workspace.py` have no import
side effect unless a discovered module imports them.

## Declare the app

```python
from druks.apps import App


class NightWatch(App):
    name = "night_watch"
    icon = "telescope"
    description = "Checks repositories after hours."
```

The class is a stateless installation singleton. Do not instantiate it. Druks mounts
every router found in its `routes` modules under `/api/night_watch`, supplies
transcript routes, and serves `druks_night_watch/dist/` under
`/app/night_watch` when it contains `entry.js`.

## Choose the right workflow shape

The parameters of `run()` or `run_multistep()` are the workflow input. Druks
builds a Pydantic model from their annotations and validates the call to
`start()`.

If the whole body is one durable operation, use `run()`:

```python
from druks.workflows import Workflow


class RecordHeartbeat(Workflow):
    async def run(self, source: str) -> None:
        Heartbeat.record(source)
```

If completed operations require independent recovery, use `run_multistep()`.
Also use it for a workflow that waits on a gate:

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

Druks treats `run()` as one step, so it must not carry `@step`.
DBOS replays `run_multistep()` orchestration, so it must not carry `@step`.
Decorate its side-effecting operations instead. An agent called directly from
the orchestration body gets its own step. An agent called inside `@step` or
`run()` shares that enclosing checkpoint.

### Declare the sandbox environment

A workflow can ship the environment its agents need as a plain shell file:

```python
from druks.sandbox import Sandbox
from druks.workflows import Workflow


class BuildSite(Workflow):
    sandbox = Sandbox(setup="sandboxes/build.sh")
```

Place the file at `site_builder/sandboxes/build.sh`. The path is relative to the
app package.

Druks reads the raw bytes. It does not render
the file or run it during import. Drukbox builds a reusable template from the
platform base and the script.
A run waits with a visible sandbox-building phase when that template is still
building. A workflow with no declaration uses the platform base unchanged.

The content hash of the base and script identifies the template. App authors do
not name provider images.

Use provider idempotency keys for writes. An interrupted operation can run again.
DBOS reuses completed checkpoints on recovery. Keep decisions in replayable
control flow. Keep I/O inside steps. See
[durability and recovery](concepts.md#durability-and-recovery).

Start a workflow with an explicit subject — an instance of the class it declares:

```python
run_id = await Sweep.start(
    subject=repository,
    repo=full_name,
)
```

A workflow without a subject declaration passes `subject=None`. A subject has
at most one active run for each workflow kind. A duplicate start returns the
active run ID. Attribution does not change this rule. Two accounts that start
the same subject share one run.

If the app requires prelaunch policy, wrap
`start()` in a domain `dispatch()` method. This method can own lookup, snapshot,
or routing policy.

A browser start attributes itself. The request identity gate records the
resolved account, and `start()` inherits it. A route does not require more
attribution code. If the dispatcher has a better account, pass `account_id`.
For example, a webhook can resolve the ticket assignee.

Each agent call uses the connection of the run account. If that connection is
absent, the call uses the installation fallback account. The call records the
charged account. Thus, you can see fallback use.

A cron or background run
without an account uses the system account. A parked run keeps its original
attribution after resume. The person who selects **Resume** does not become the
payer.

### The journal

Druks keeps a journal of the typed values for each run. Each body-level agent
call and gate reply enters it in call order. Add your values with
`self.journal.add()`. Read them by contract type:

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

The journal survives crashes without separate storage. Recovery runs the body again
with every durable call memoized, so the same entries land in the same order.

Two rules:

- Druks journals only body-level calls. An agent call inside a `@step` — or in
  a `run()` body, which is one big step — never enters it. Keep that state
  in local variables.
- Never mutate body-held state inside a `@step`. DBOS skips a completed step
  on replay, so the write disappears.

### Announcing domain events

If another component must react to a body action, announce the action:

```python
await self.announce("pr.opened", pr_number=delivery.pr_number, branch=delivery.branch)
```

The platform routes it to subscribers that filter on your workflow and subject.
The publication is a durable checkpoint. Recovery does not publish it again.
Announce from the body, not inside a `@step`.

### Schedules and settings

Set `every` to declare a cron:

```python
class Sweep(Workflow):
    every = "0 6 * * *"
```

The tick fires the workflow's body with no subject and no input, so every body
parameter needs a default. A workflow whose runs are *about* something (it
declares a `subject`) must not start that way. Give it a `dispatch()` classmethod
and the schedule fires that instead — it resolves the subject and starts the
real run:

```python
class Engage(Workflow):
    subject = Account
    every = "0 */4 * * *"

    @classmethod
    async def dispatch(cls) -> str:
        return await cls.start(subject=Account.get())

    async def run(self) -> None:
        ...
```

A scheduled `dispatch()` fires with no arguments, so it must be nullary. Druks
evaluates cron expressions in the operator timezone. The dashboard can retune or
disable a declared schedule but cannot invent a new workflow schedule.

### Background tasks

A `Workflow` is the right home for work you want on a subject's timeline — a run
with agent calls, gates, and operator-tunable settings. Plumbing that wants none
of that — periodic maintenance, a fire-and-forget side effect — is a `task`:

```python
from druks.workflows import task


@task(every="*/15 * * * *")
async def refresh_tokens() -> None:
    ...


@task(retries=4)
async def sync_labels(pull_request_id: int) -> None:
    ...
```

Call `await sync_labels.enqueue(pull_request_id=7)` from a route, subscriber, or
workflow body. Never call it inside a `@step`. Like a workflow, the signature is
the wire contract. Parameters are
annotated. `enqueue()` validates them and stores JSON. A task keeps no run row
and never reaches the timeline.

It has no subject, gate, or operator settings.
It cannot make agent calls. `every=` uses a fixed UTC cadence that the code
owns. An operator can retune the `every=` value of a workflow.
`retries=` sets retries after the first attempt, both here and on `@step`.

A workflow can declare its own operator settings:

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

An agent belongs to the app class. Its family default (`claude` or `codex`)
uses the related operator harness setting. A full model name fixes the default.

```python
from druks.agents import Agent, AgentOutput


class ReportOutput(AgentOutput):
    title: str
    body: str


class NightWatch(App):
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

Druks renders the prompt with the current workflow, workspace, and supplied
context. The selected harness provisions or attaches a sandbox, executes the
CLI, validates the structured output, and records the call. Override
`AgentOutput.to_result()` to map the strict agent contract to a domain value.
Override `get_artifact()` to publish a reviewable artifact.

If an agent produces a file, use [`File` and `FileField`](files.md).
The contract declares the file, Druks transports and serves it, and the app can
persist its stable reference on an app row.

Do not ask the framework to infer domain side effects from agent prose.
The prompt or a subsequent explicit step owns those actions.

## Customize the workspace

Every agent uses a `Workspace` around a Drukbox sandbox. Override
`Workflow.workspace_class` and `get_workspace_kwargs()` for app-specific
workspace behavior. This behavior can clone a repository, mint a short-lived
token, or require an MCP server.

Keep durable state outside the VM. A workflow can set
`steps_reuse_sandbox = True` to retain one host across a segment. Druks releases
the host at a gate and at workflow exit. It rotates the host near lease expiry.

### Borrow a browser session

Declare required logins on the app class. The attribute name and app name form
the session identity. The sessions pane asks the operator to sign in:

```python
from druks.browser import BrowserSession
from druks.apps import App


class NightWatch(App):
    name = "night_watch"
    acme = BrowserSession(site="acme.example", persist=True)
```

A workflow borrows the logged-in browser as a Playwright handle. The app
declares Playwright as its dependency. Druks owns the browser container and its
lifecycle. The browser starts in a container on the Druks host. It stops with
the block. Druks exports and stores a ``persist`` session before the stop:

```python
async with NightWatch.acme.playwright() as browser:
    page = await browser.new_page()  # opened on the logged-in context
    await page.goto("https://acme.example/home")
```

``playwright()`` yields the logged-in browser context. Pages that you open in
this context use the session. ``NightWatch.acme.cdp()`` borrows the same browser
and yields the raw CDP URL. Use this URL with a test suite, raw CDP client, or
custom wrapper.

``persist=True`` writes rotated state after each borrow. Use it for sites that
expire an unused login. ``headless=True`` is an optional optimization for sites
that do not fingerprint headless browsers.

``anonymous=True`` declares a session that needs no login. A borrow opens a
browser with an empty profile. The operator does not sign in. Use this option
for a public target or app-owned credentials. These credentials can be an
identity header or a token in the URL. An anonymous session stores no state.
Thus, ``persist=True`` with it fails during class definition.

If a borrowed browser returns to the login page, raise
``BrowserSessionSignedOutError`` from ``druks.browser``. Druks marks the session
as stale. The sessions pane shows this state and stops new borrows. The run
fails with the same reason. After the operator signs in again, the next
scheduled run can proceed.

Provider selection is an operator concern. App workspace code targets the
Druks sandbox contract, not `exe`, AWS, or Docker directly.

## Wait for input

The gate fields form the reply schema. `name` fixes the durable gate identity.
This identity selects the receive channel and the `gate` value of the parked
run. You must declare it because the identity must survive a class rename:

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
workflow through the gate and its subject:

```python
await ApproveReport.answer(
    repository,
    action="approve",
    note="Ship it.",
)
```

`answer()` resolves the subject run that waits on the gate. It raises if no such
run exists. This includes a gate that has an answer or timeout. A subject can
have runs from several workflows. The gate identifies the applicable run.

For a subject-backed decision inside the Druks dashboard, use:

```python
reply = await self.review(questions=report.questions, context=review_context)
```

It offers `approve` and `request_changes`. Druks shows optional nonblank
`context` next to the review. With this context, `request_changes` does not
require answers or a note. Authors must treat that response as another pass and
include the context. A subjectless workflow cannot use in-app review. A
subjectless custom gate must override `on_wait()` to show the wait.

Without this
override, Druks raises an error instead of a silent park.

Raise `FatalError` for a deliberate domain stop. If readers need a stable
machine failure code, subclass it. Set `code` on the subclass. Unexpected exceptions fail
the run and are re-raised to DBOS.

Stop this workflow's active execution for a subject through the workflow class:

```python
await Sweep.cancel(repository)
```

The workflow class supplies its kind. The caller does not find or handle the
internal timeline row. A cancel request with no active run has no effect. Thus,
a redelivered webhook stays idempotent.

## Give runs a subject read-side

A subject is what your runs are about — a repository, a work item, a pull
request. It is always a class, and the workflow names it:

```python
class Sweep(Workflow):
    subject = Repository
```

This declaration lets Druks show subject history and available actions before a
run exists. `start()`, `cancel()`, and `Gate.answer()` enforce the declaration.
A workflow with a subject starts with an instance of that class. A workflow
without a subject passes `subject=None`.

When the subject is a row you keep — one you list, edit, and show fields from —
subclass `StoredSubject` instead of `Base`. The class name is the subject type:
`Repository` becomes `repository`.

```python
from druks.db import StoredSubject


class Repository(StoredSubject):
    __tablename__ = "repositories"

    def get_label(self) -> str:
        return self.full_name

    @classmethod
    def list_summaries(cls, account_id: str | None) -> list[SubjectSummary]:
        return [repository.get_summary() for repository in cls.list_open()]
```

Select the rows for the board. Druks supplies the other behavior. Each subject
already supplies its ID and `label`. The label is its one-line description. If
the board requires more fields, add a custom summary:

```python
from druks.workflows import SubjectSummary


class RepositorySummary(SubjectSummary):
    open_findings: int


class Repository(StoredSubject):
    def get_summary(self) -> RepositorySummary:
        return RepositorySummary.model_validate(self)
```

If you keep no row for a subject, subclass `Subject`. The platform requires only
an identity. The ID is the full record and its label:

```python
from druks.workflows import Subject


class PullRequest(Subject):
    @classmethod
    def list_summaries(cls, account_id: str | None) -> list[SubjectSummary]:
        return [pull_request.get_summary() for pull_request in cls.list_open()]
```

Each ID names one of these subjects, so a detail read always answers. Override
`get_for_subject_id()` to reject an invalid shape. For example,
`owner/repo#7` is a pull request and `nonsense` returns a 404.

Each subject a workflow declares must implement `list_summaries()`. The board
reads it and passes the caller. `account_id` is the signed-in account, or None
outside a request. If each operator has a separate board, use it to scope the
rows. If all operators share one board, ignore it.

A model method never
reads request context. Druks validates the method at load. If it is missing, the
app does not load. The error names the app, the subject, and the
method.

Druks serves the same `/api/night_watch/repository` surface for both subject
types. This surface contains a board, detail pages, and a live stream. Druks
mounts it for each declared subject. Each response contains your summary, run
status, timeline, agent calls, artifacts, and active question. Override
`get_subject_activity()` only to add transient app detail, such as
"Building sandbox VM…".

Pass the subject instance to each component that requires one. This includes a
workflow start, gate answer, or event:

```python
await NightWatch.dispatch(subject=repository)
```

Inside the workflow, `self.subject` resolves through the declared class. It is
live, not a snapshot from dispatch. A run can park on a gate for three days.
After resume, it reads the current row. If the row no longer exists, the subject
does not resolve.

Your app names domain outcomes. For example, a work item ships or an operator cancels it.
Druks owns the active run state. Read this state from the status:

```python
status = repository.get_status()
if status.is_parked:
    ...  # a run stopped to ask a human something
```

`status.kind` names the workflow currently driving the row and `status.gate` the
question it stopped on. While a run is active, `await repository.get_phase()`
returns the step it is on.

## Record events and react to signals

Record an event through the app. Druks stamps its ownership:

```python
NightWatch.record_event(
    type="report.published",
    subject=repository,
    payload={"url": report_url},
)
```

`type` is the milestone word that the feed reads. There is no presentation hook
to implement. Lifecycle events for subjected workflows are
recorded automatically. Call `record_event()` inside a platform-bound
transaction such as a request, durable step, or subscriber.

A feed row contains facts, not prose. It contains its kind, workflow, subject
identity, and event payload. A client supplies the words. Give the subject a
``label`` for its one-line description. Each later event for the subject keeps
that label:

```python
class Repository(StoredSubject):
    def get_label(self) -> str:
        return self.full_name
```

React with filters rather than body guards:

```python
from druks.signals import subscribe
from druks.workflows import WorkflowEvent


@subscribe(WorkflowEvent.FINISHED, subject=Repository)
async def on_sweep_finished(*, subject: Repository, **_: object) -> None:
    await notify(subject.full_name)
```

`subject=Repository` selects each workflow for a repository. `workflow=Sweep`
selects one workflow and its declared subject. Do not use both filters together.
The subscriber body receives its subject with either filter.

Signals deliver at least one time. A subscriber exception propagates. Then the
webhook provider or DBOS retries the publication. Make each reaction idempotent.

## Receive webhooks

A webhook authenticates and normalizes provider input. It must publish a
domain-neutral signal rather than contain workflow policy:

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

Models subclass `druks.db.Base` and every normal app table starts with
`<name>_`:

```python
from sqlalchemy.orm import Mapped, mapped_column

from druks.db import Base


class Report(Base):
    __tablename__ = "night_watch_reports"

    id: Mapped[int] = mapped_column(primary_key=True)
```

Generate the app's revision after the model is importable:

```bash
uv run druks makemigrations night_watch -m "add reports"
uv run druks init-db
```

Druks scopes autogeneration to the table prefix and writes the version to
`alembic_version_night_watch`. Query through `druks.db.db_session()` inside an
HTTP request, durable step, or other platform-bound session.

HTTP response models subclass `druks.schemas.BaseResponse`, whose snake_case
fields serialize as camelCase. Request models are ordinary Pydantic models.
Druks mounts each router from a discovered `routes.py` below the app namespace.
It tags the router with the app name. A router declares only the prefix of its
resource:

```python
router = APIRouter(prefix="/reviews")
```

Your routes require authentication. The loader puts each router behind the
platform identity gate. The gate accepts a Bearer PAT or the signed-in session.
The gate blocks anonymous requests before your code. You do not implement authentication.
If a route uses account scope, read the caller:

```python
from druks.accounts import current_account_id

@router.get("/reviews")
def list_reviews() -> list[ReviewResponse]:
    return Review.list_for_account(current_account_id.get())
```

Tag a route with `agent` to create an MCP tool from it. Give the route an
explicit `operation_id`. Druks prefixes this value with the app name. For
example, `operation_id="add_peer"` in `peer_tracker` becomes
`peer_tracker_add_peer`. The docstring supplies the description.

A `GET` route is read-only. If a write is non-destructive, declare
`x-destructive: false`. If a write is idempotent, declare `x-idempotent: true`.
Safe defaults are destructive
and non-idempotent. Startup refuses a missing `operation_id` or docstring.

Two spellings run through druks, and which one a segment wears says who owns it:

| | |
| --- | --- |
| `snake_case` | an identity the platform serves — your app name, a subject type |
| `kebab-case` | a resource you named — your route prefixes, your frontend paths |

Thus, `/api/review/pull_request` is the subject board for review runs.
`/api/review/reviews` is the resource that your POST creates. The platform
matches `<subject_type>`, `transcripts`, and `pages` before your routers. A
custom router cannot take a platform read, including through a catch-all.
`transcripts` and `pages` are reserved: a subject type or a router prefix that
takes one fails the load. Name the router for its resource to prevent a
conflict.

## Declare a service

A service identity is the appliance registration at an external provider. A
deployment has one identity for each service string. The platform GitHub App is
the first service identity. OAuth grants are not service identities. The
platform stores them after an operator connection.

See [Connect provider accounts](#connect-provider-accounts-oauth).

Put a credential that only your app uses in its app settings.

Declare one class in `services.py`. The platform creates the connection card in
Settings. It validates and stores the submitted values. It encrypts
`SecretStr` fields and stores plain fields as identity facts.

It also reports the state through `druks doctor`. The class name is the identity.
Druks derives the slug
from it (`Gmail` → `gmail`, `GoogleCalendar` → `google_calendar`) and derives
the card heading from the slug:

```python
from pydantic import BaseModel, Field, SecretStr

from druks.services import Service, ServiceConnectError


class Gmail(Service):
    description = "The appliance's own OAuth client — every mailbox authenticates against it."

    class Settings(BaseModel):
        client_id: str = Field(title="Client ID")
        client_secret: SecretStr = Field(title="Client secret")
```

The slug keys the `service_identities` row and the connect wire. A class
rename changes the slug, rekeys the card, and orphans the connected identity.
Set `slug = "gmail"` on the class to keep the old key.

Read it back through the same class:

```python
Gmail.get().secrets["client_secret"]   # raises ServiceNotConnectedError when unset
Gmail.is_connected()
```

An optional `verify` classmethod proves the paste against the live provider
before anything replaces a working identity. It returns extra identity facts
to store, and raises `ServiceConnectError` with a message safe to show — the
platform never echoes what the operator pasted:

```python
@classmethod
async def verify(cls, settings: Settings) -> dict:
    if not await probe(settings):
        raise ServiceConnectError("The provider did not accept these credentials.")
    return {}
```

If the appliance is healthy without the service, set `required = False` on the
class. Doctor then reports a note instead of pending setup.

Key the service for the integration that your app consumes (`Gmail`), not the
provider (`Google`). A second integration on the same provider declares its own
service. The operator decides whether each card uses a shared or narrow
registration. This choice controls scope and the effect of a credential problem.

## Connect provider accounts (OAuth)

Declare the OAuth endpoints on the service that holds the client
credentials. The `Settings` model must have `client_id` and `client_secret`
fields:

```python
class Acme(Service):
    authorization_endpoint = "https://acme.example/oauth/authorize"
    token_endpoint = "https://acme.example/oauth/token"
    # True = HTTP Basic on the token endpoint. False = secret in the body.
    basic_auth = True
    identity_endpoint = "https://acme.example/oauth/userinfo"
    identity_scopes = ("openid", "email")
    # Query parameters the provider's consent URL must carry.
    extra_authorize_params = {"access_type": "offline", "prompt": "consent"}

    class Settings(BaseModel):
        client_id: str = Field(title="Client ID")
        client_secret: SecretStr = Field(title="Client secret")
```

`extra_authorize_params` declares special consent-query values for the provider. The
platform adds them to every sign-in it starts for the service. The example
shows Google's: it grants a refresh token only when the consent asks for
`access_type=offline` with `prompt=consent`.

`identity_endpoint` names the provider endpoint that returns the signed-in
account's facts (email, username, name). Druks calls it once at consent and
shows the facts as the connection label in Settings. `identity_scopes` are the
scopes that this call requires. Druks adds them to the consent request.

If one identity fact names the provider account, declare `identity_key`:
`"sub"` for Google, `"id"` for GitHub. A fresh sign-in that matches an
existing connection for the same owner updates that row instead of
creating a second one. A revoked row that matches becomes live again and
keeps its id. Without the declaration, each fresh sign-in creates a new
connection.

Some providers have no such endpoint, or return the facts in a different
shape. Override `get_identity` for them:

```python
    @classmethod
    async def get_identity(cls, access_token: str) -> dict:
        payload = await fetch_profile(access_token)
        return payload["data"]
```

One provider can back several services — Google backs both Gmail and Google
Calendar, and each keeps its own card and its own key. Share the provider's
declarations through an abstract base. Set `abstract = True`. The base never
registers. Each subclass inherits everything it declares, `Settings`
included, and needs nothing beyond its class name:

```python
class GoogleOauth(Service):
    abstract = True
    authorization_endpoint = "https://accounts.google.com/o/oauth2/v2/auth"
    token_endpoint = "https://oauth2.googleapis.com/token"
    extra_authorize_params = {"access_type": "offline", "prompt": "consent"}
    identity_endpoint = "https://openidconnect.googleapis.com/v1/userinfo"
    identity_scopes = ("openid", "email")
    identity_key = "sub"

    class Settings(BaseModel):
        client_id: str = Field(title="Client ID")
        client_secret: SecretStr = Field(title="Client secret")


class Gmail(GoogleOauth):
    pass


class GoogleCalendar(GoogleOauth):
    pass
```

Declare your app's use of the service, with the scopes your calls
need:

```python
class NightWatch(App):
    name = "night_watch"
    acme = Acme.with_scopes("profile.read", "posts.write")
```

A *connection* is one signed-in provider account. A user can have several
connections for each provider. Each mailbox, handle, or workspace can have one.
The platform stores the refresh token, granted scopes, and owner for each
connection. Workflow code reads them through the declaration. It gets one token
for each connection:

```python
for connection in NightWatch.acme.list_for_account(account_id):
    token = await connection.get_access_token()
```

`account_id` is the caller: `self.account_id` in a run body,
`current_account_id.get()` in a route, the handler's argument in a
subscriber, the platform's argument in `list_summaries`. `NightWatch.acme.get(connection_id)` returns one connection
when your own row stored its id. Each connection carries `id`, `scopes`, `identity` — the
provider's facts for the sign-in — `account_id` — the druks account that
signed it in — and `connected_at`. The handle serves
live connections only. A revoked connection drops out of `get` and
`list_for_account`, but its platform row survives with its owner and
identity. Your rows never need tombstone copies of either.

Your UI starts a sign-in by opening `/api/oauth/acme/connect`. The platform
requests the combined scopes from each installed app. It stores the connection
for the signed-in user. A fresh
sign-in creates a new connection, unless the service's `identity_key`
matches it to an existing connection for the same owner.

To widen an existing connection's scopes, open
`/api/oauth/acme/connect?connection=<id>`. Reconsent replaces its tokens.
Reconsent names the row, so it also makes a revoked connection live again
under its old id. A fresh sign-in that matches the `identity_key` does the
same.

Add `?next=/app/night_watch/accounts` to land the user back on your
page after consent instead of the generic "connected" page. `next`
must be a bare path that starts with `/`. Druks rejects a URL with a scheme or
host. Thus, the connection flow cannot redirect away from the host. Register
`https://<host>/api/oauth/callback` as the provider redirect URI. It serves each
service.

React to sign-ins with the signal machinery. The platform publishes
`oauth.connected` when a consent completes. `reconsent` is true when the
consent replaced an existing connection's tokens. This happens on
reconsent by id and on an `identity_key` match, for a live or a revoked
row.

It publishes `oauth.disconnected` after a user revokes a connection. A
replacement of the service's client credentials also publishes this signal.
Revocation is a state, not a deletion: your subscriber can still read the
connection it is told about. Subscribe in `subscribers.py`:

```python
from druks.signals import subscribe


@subscribe("oauth.connected", provider="acme")
async def adopt_sign_in(
    provider: str, connection_id: str, account_id: str, reconsent: bool
) -> None:
    if not reconsent:
        WatchedAccount.adopt(connection_id, account_id)


@subscribe("oauth.disconnected", provider="acme")
async def drop_sign_in(provider: str, connection_id: str, account_id: str) -> None:
    WatchedAccount.drop(connection_id)
```

The user sees and revokes everything in Settings — every connection they
hold, across services, revoked ones shown as history. Replacing a service's
client credentials revokes its connections: a new client can never refresh
the old client's tokens, but the consents stay on record.

`get_access_token` serves a Redis-cached access token and lets only one
refresher run per connection and scope set. This is necessary: two
refreshes at the same time can make the provider revoke the whole
connection. It raises `OauthRefreshError` when the refresh fails. Then ask
the user to reconnect.

`get_access_token(scopes=("profile.read",))` asks the provider for a token with
fewer scopes than the grant. If the token goes to untrusted compute, use it.
Pass a subset of the connection scopes. `cached=False` bypasses the cache to get
a full-lifetime token.

## App settings and checks

An inner `AppSettings` class defines dashboard-editable knobs and owns their
cross-field coherence:

```python
from typing import Literal

from pydantic import Field

from druks.apps import App, AppSettings, Secret


class NightWatch(App):
    name = "night_watch"

    class Settings(AppSettings):
        provider: Literal["none", "acme"] = "none"
        service_token: Secret = Field(
            json_schema_extra={"section": "Acme", "visible_when": {"provider": "acme"}},
        )
        webhook_secret: Secret = Field(
            title="Webhook secret",
            json_schema_extra={"section": "Acme", "visible_when": {"provider": "acme"}},
        )

        def clean(self) -> dict[str, str]:
            if self.service_token and not self.webhook_secret:
                return {"webhook_secret": "Required once the service token is set."}
            return {}
```

Supported display shapes are scalar values, `Literal` choices, and
`SecretStr`, including optional forms. Druks rejects nested Pydantic models.
It redacts secret values and submitted validation errors. Declare a secret
field as `Secret`.

An unset field is an empty, false `SecretStr`. Thus,
`if self.service_token:` reads its state without a guard for
`.get_secret_value()`. A multiline secret, such as a PEM private key, can use
`json_schema_extra={"multiline": True}`. The settings form shows a textarea and
keeps newlines. Storage, redaction, and write-only behavior do not change.

`section` is a plain heading that Druks renders in first-declaration order, with
unsectioned fields first. `visible_when` takes one same-model `{field: value}`
equality condition. Its controller must be non-secret and unconditional, and a
`Literal` controller requires one of its declared members.

Hidden fields keep their stored values. Read the resolved model with
`NightWatch.settings()`. The settings form runs `clean()` against the
resolved settings after the proposed edits and rejects an incoherent save. `druks doctor`
runs the same method over stored settings so rows from older releases or manual database
edits remain visible. Workflow settings stay plain Pydantic `BaseModel` declarations.

An app can add precondition checks through `checks`. These checks supplement
settings validation. Return `druks.doctor.CheckResult`. Druks namespaces the
result. It converts an exception or malformed result into an error without
hiding later checks.

## Test an app

The Druks installation registers its pytest plugin. An app can request the
fixtures directly without a `conftest.py` or `pytest_plugins` declaration:

| Fixture | Contract |
| --- | --- |
| `druks_db` | A SQLAlchemy `Session` bound to a per-test transaction. Commits become savepoints, and teardown rolls the outer transaction back. |
| `druks_client` | An authenticated `TestClient` with installed apps mounted, sharing `druks_db`'s connection. |
| `druks_redis` | The test Redis database, flushed before the test. |
| `druks_without_dispatch` | Workflow starts and run-phase writes become no-ops, for tests that stand up no durable engine. |
| `druks_without_remote_config` | Every `.druks` namespace lookup misses, so prompts resolve to bundled templates and config to its declared defaults. |

The fixtures are not autouse. A test that requests `druks_client` also gets
`druks_db`. A test accesses Redis only if it requests `druks_redis`.

Run a workflow's body against a subject with no durable engine — no checkpoints,
no lifecycle events, no retries:

```python
from druks.testing import run_workflow

summary = await run_workflow(Sweep, subject=repository, since="2026-07-01")
```

`@step` calls inside a `run_multistep` body still need the real engine.

Seed platform-owned run and agent-call rows with plain functions:

```python
from druks.testing import seed_call, seed_run

run = seed_run(
    druks_db,
    kind=Sweep.kind,
    subject=repository,
    state="running",
)
call = seed_call(
    druks_db,
    run,
    NightWatch.report,
    status="running",
)
```

`seed_run` writes both the run row and its DBOS workflow status. That status
determines `Run.state`. `seed_run` requires `kind`. If you seed
`state="pending_input"`, pass `input_gate`. `seed_call` accepts an `Agent` or
its string ID.

`make_settings(tmp_path, **overrides)` builds isolated Druks settings.
`configure_app_for_test(settings=..., authenticated=False)` returns the mounted
app if a test needs its own client or an unauthenticated request path.
`druks_client` covers the normal authenticated case.

The fixtures never read the runtime's settings. They read
`DRUKS_TEST_DATABASE_URL` and `DRUKS_TEST_REDIS_URL`. The defaults are a local
`druks_test` database and Redis index 15. The fixtures point the code under test
to the same pair. Thus, a run cannot use the values in `DRUKS_DATABASE_URL` or
`DRUKS_REDIS_URL`.

Create the database one time with `createdb druks_test`. The
development Compose project already creates it.

On that database, the plugin creates `citext` and imports installed app models.
It runs SQLAlchemy `create_all` and seeds platform reference rows. It builds the
DBOS system tables through DBOS database migrations. It does not reset or drop a
schema. It rolls back each test write through `druks_db`. `druks_redis`
runs `FLUSHDB` on the test index.

## Declare pages

An app declares its screens in Python and ships no JavaScript. Pages live in
`pages.py`:

```python
from druks import ui


@ui.page("/")
async def reports():
    return ui.Page("Reports", description="Every sweep this install ran.")


@ui.page("/peers/{peer_id}")
async def peer(peer_id: int):
    return ui.Page(title=f"Peer {peer_id}")


@peer.child("/history")
async def peer_history(peer_id: int):
    return ui.Page(title=f"Peer {peer_id} history")
```

`@page` declares a top-level page. `@parent.child` declares a page under it,
one level deep. A page function takes one parameter for each parameter of its
route, so a child inherits its parent's. It needs no return annotation.

Exactly one page declares `/`. That page is the one the app opens on. A static
child renders as a tab on its parent. A parameterized child is a detail page a
`Link` reaches.

The page name is the function name. The page label is that name with its
underscores as spaces, and `label=` overrides it.

`App.pages()` enumerates the pages in route-match order: literal segments
before parameters, at every depth. Declaration order never decides a match.

`navigation` names the pages the appbar shows as subnav tabs, in order. Each
one must be a static top-level page. The tab wears the page's label, so an app
never spells a label twice:

```python
class NightWatch(App):
    name = "night_watch"
    navigation = ["reports"]
```

Druks checks the whole table at boot. A missing landing page, a repeated page
name, a nested child, two routes a request could not tell apart, a signature
that does not match its route, or a navigation entry that is not a static
top-level page fails the load, with the app name and the exact cause.

### A page function is a pure read

Druks reruns a page function on initial load, on an event, on reconnect, on a
manual refresh, and on a retry. The call count and the call order are not
guaranteed. These are the rules.

A page function can:

- read Druks state,
- read the app's own data,
- read a projection,
- read a read-only external source.

A page function must not:

- write data,
- start or enqueue work,
- publish an event,
- answer a gate,
- cause an external effect,
- depend on mutable process state.

Write the function so that a repeat call is free. Put every write behind an
operation the operator triggers.

### Watch a subject

A page, or a named region inside it, declares the subject it `follows`. Druks
streams that subject through the read side every app already gets, and the
shell rereads the page on each snapshot:

```python
@ui.page("/notes/{note_id}")
async def note(note_id: int):
    found = await Note.get(note_id)
    status = await found.get_status()
    if status.gate:
        decision = [ui.GateControls(status.run)]
    else:
        decision = [ui.Text("Nothing is waiting on you.")]
    return ui.Page(
        title=f"Note {note_id}",
        blocks=[ui.Section(title="Your decision", name="decision", follows=found, blocks=decision)],
    )
```

The shell replaces the named region and leaves the rest of the page alone, so
scroll position, focus, and half-filled inputs outside it survive. A region
that follows a subject must have a name; that is how the shell finds it.

`GateControls` names only the run. The shell reads the ask, its options, its
context, and its artifact from the parked run, and submits the operator's
answer with the run's `parkedAt`. A `GateControls` block must sit inside
something that follows a subject, or an answered gate would stay on screen.

The [Druks UI contract](druks-ui.md) holds the block, value, and field catalog,
actions, and liveness.

## Frontends

An installed app is visible in the dashboard without a custom UI. The shell
reads the installed roster from `/api/apps`. It gives each app an entry in the
app switcher and generic pages. Each subject type gets a board. Each subject
gets a page with its timeline, transcripts, and gate controls. The subject
summary fields form the board row.

No additional declaration is necessary.
The shell derives the switcher label from `name` (underscores become spaces).

An app that needs full control of its interface ships a frontend instead. Its
pages are its own JavaScript, so it declares its own tabs there and leaves
`App.navigation` empty.

The frontend is an ES module that the shell mounts
inside its own document, below the chrome. The scaffold ships a placeholder
`druks_night_watch/dist/entry.js`. Set the frontend build output to that `dist/`
directory. The contract uses `shellApi: 1`:

- **Entry module:** `entry.js` exports `shellApi = 1` and `mount(el, ctx)`. The function renders into
  `el` and returns a dispose function. A missing `mount` or a version mismatch
  renders a visible error panel in the shell.
- **Context:** `ctx` carries `apiBase` (`/api/<name>`), `navigate(path)` for shell-side
  navigation, `theme.accent`, and `markdown(source)` — the shell's own
  markdown renderer, so an app does not bundle one. The app renders in the
  shell's document, so the shell's CSS variables cascade into it. The shell
  re-broadcasts every location change as a `popstate` event while the app is
  mounted.
- When `dist/style.css` is present, the shell loads it while it mounts the app.

Build the bundle in Vite library mode. Keep `react`, `react-dom`,
`react-dom/client`, and `react/jsx-runtime` external. The shell import map
resolves them to its copy. Thus, one React instance serves the document.
Bundle other dependencies as usual. Route by reading `location.pathname` under
`/<name>/`.

The bundled Druks SPA also has a shared React app registry. To join this shell,
compile the app UI module into the dashboard image. An installed Python wheel
cannot change an existing JavaScript bundle. See the
[frontend guide](https://github.com/czpython/druks/blob/main/frontend/README.md)
for that in-repository path.

## Stable author imports

Import from concern namespaces, not from `druks.durable` or internal modules:

| Namespace | Public names |
| --- | --- |
| `druks.accounts` | `current_account_id` |
| `druks.apps` | `App`, `AppSettings`, `Secret` |
| `druks.services` | `Service`, `ServiceConnectError`, `ServiceNotConnectedError`, `OauthClient`, `OauthExchangeError`, `OauthRefreshError` |
| `druks.secrets.fields` | `EncryptedJsonField`, `SecretsMapping` |
| `druks.agents` | `Agent`, `AgentOutput` |
| `druks.workflows` | `Workflow`, `Gate`, `step`, run/agent response types, lifecycle enums and workflow errors |
| `druks.sandbox` | `Sandbox` |
| `druks.db` | `Base`, `StoredSubject`, `db_session` |
| `druks.schemas` | `BaseResponse` |
| `druks.ui` | `Block`, `Callout`, `Card`, `Chart`, `ChartSeries`, `Columns`, `Divider`, `EmptyState`, `Fact`, `Facts`, `FileSummary`, `Files`, `Follows`, `GateControls`, `Image`, `ImageGallery`, `Link`, `List`, `Markdown`, `Metric`, `Metrics`, `NumberValue`, `Page`, `Progress`, `ProgressStep`, `Section`, `Stack`, `StatusValue`, `Table`, `TableColumn`, `TableRow`, `Text`, `TextValue`, `TimeValue`, `Timeline`, `TimelineItem`, `Value`, `page` |
| `druks.signals` | `subscribe` |
| `druks.events` | `Event` |
| `druks.files` | `File`, `FileField` |
| `druks.prompts` | `render_prompt` |
| `druks.webhooks` | `Webhook`, `verify_hmac_sha256` |
| `druks.testing` | `run_workflow`, `seed_run`, `seed_call`, `init_db` — plus the fixtures the plugin registers |

The root `druks` package deliberately exports only its version.
