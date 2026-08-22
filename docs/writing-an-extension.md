# Writing an extension

An extension is an application installed into Druks as its own Python
distribution. It owns domain behavior; Druks supplies durable execution and
shared operating services. Read [the extension boundary](concepts.md#the-extension-boundary)
before choosing which side should own a capability.

## Scaffold and prove the package

```bash
uvx --from druks druks create extension night_watch
cd druks-night_watch
uv sync
uv run pytest
```

From a Druks checkout, `uv run druks create extension night_watch` scaffolds with
that checkout's CLI instead.

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

The project root also carries an `AGENTS.md` holding the contracts a coding agent
cannot infer from the stubs, and a link back to this guide.

The scaffold depends on the published `druks`. To develop an extension against a
local checkout instead, pin it:

```toml
[tool.uv.sources]
druks = { path = "../druks", editable = true }
```

## Package layout

The scaffold separates self-registering capability modules from ordinary
application modules:

| Path | Contract |
| --- | --- |
| `extension.py` | `Extension` subclass, agents, extension settings |
| `workflows.py` | durable `Workflow` and `Gate` subclasses |
| `models.py` | SQLAlchemy models with `<name>_` table names, `StoredSubject` among them |
| `contracts.py` | `AgentOutput` contracts |
| `schemas.py` | HTTP responses and subject summaries |
| `routes.py` | FastAPI routers |
| `subscribers.py` | signal reactions |
| `webhooks.py` | authenticated provider deliveries; add when needed |
| `services.py` | `Service` declarations — the appliance's own credentials at external providers; add when needed |
| `migrations/versions/` | this distribution's Alembic history |
| `dist/` | optional built frontend module, mounted inside the shell (served under `/app/<name>`) |

Druks recursively discovers leaf modules named `workflows`, `routes`,
`subscribers`, `webhooks`, and `services`. A capability hidden in `workflow.py` is not
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
transcript routes, and serves `druks_night_watch/dist/` under
`/app/night_watch` when it contains `entry.js`.

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

Start a workflow with an explicit subject — an instance of the class it declares:

```python
run_id = await Sweep.start(
    subject=repository,
    repo=full_name,
)
```

A workflow that declares none passes `subject=None`. A subject has at most one
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

### Announcing domain events

When another component should react to something the body just did, announce it:

```python
await self.announce("pr.opened", pr_number=delivery.pr_number, branch=delivery.branch)
```

The platform routes it to subscribers filtering on your workflow and subject, and
the publish is its own durable checkpoint — a recovery replay does not re-fire it.
Announce from the body, not inside a `@step`.

### Schedules and settings

Set `every` to declare a cron:

```python
class Sweep(Workflow):
    every = "0 6 * * *"
```

The tick fires the workflow's body with no subject and no input, so every body
parameter needs a default. A workflow whose runs are *about* something (it
declares a `subject`) shouldn't fire that way. Give it a `dispatch()` classmethod
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

### Borrow a browser session

Declare the logins your extension needs on the Extension class; the attribute
name and the extension's name become the session's identity, and the sessions
pane asks the operator to sign in:

```python
from druks.browser import BrowserSession
from druks.extensions import Extension


class NightWatch(Extension):
    name = "night_watch"
    acme = BrowserSession(site="acme.example", persist=True)
```

A workflow borrows the logged-in browser as a playwright handle — the
extension declares playwright as its own dependency, and druks owns
everything else (the browser boots in its own container on the druks box and
dies with the block; a ``persist`` session is exported and stored back
first):

```python
async with NightWatch.acme.playwright() as browser:
    page = await browser.new_page()  # opened on the logged-in context
    await page.goto("https://acme.example/home")
```

``playwright()`` yields the logged-in browser context; pages you open on
it carry the session. ``NightWatch.acme.cdp()`` is the same borrow yielding the raw CDP
url, for any other client — an existing test suite, raw CDP, your own
wrapper.

``persist=True`` writes rotated state back after each borrow — for sites that
expire an unused login. ``headless=True`` is an opt-in optimization for sites
that don't fingerprint headless browsers. When your code inside the borrow
sees the site bounce the login, raise ``BrowserSessionSignedOutError`` (from
``druks.browser``) and druks does the rest: the session goes stale — the pane
shows it and refuses further borrows until the operator signs in again — and
the run fails under that reason. There is nothing to catch; the next
scheduled run proceeds once the login is back.

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
workflow through the gate and its subject:

```python
await ApproveReport.answer(
    repository,
    action="approve",
    note="Ship it.",
)
```

`answer()` resolves the subject's run parked on that gate, and raises when none
is — an already-answered or timed-out gate included. A subject may have runs of
several workflows at once; the gate identifies which one answers.

For a subject-backed decision inside the Druks dashboard, use:

```python
reply = await self.review(questions=report.questions, context=review_context)
```

It offers `approve` and `request_changes`. Optional non-blank `context` is rendered
beside the review and permits `request_changes` without answers or a note; authors
must treat that response as another pass that folds the context in. A subjectless
workflow cannot use in-app review. A subjectless custom gate must override
`on_wait()` so the wait is visible; otherwise Druks raises instead of parking
silently.

Raise `FatalError` for a deliberate domain stop. Subclass it and set `code`
when readers need a stable machine failure code. Unexpected exceptions fail
the run and are re-raised to DBOS.

Stop this workflow's active execution for a subject through the workflow class:

```python
await Sweep.cancel(repository)
```

The workflow class supplies its kind; the caller never locates or handles the
platform's internal timeline row. Cancelling a subject with nothing of that kind
running is a no-op, so a redelivered webhook stays idempotent.

## Give runs a subject read-side

A subject is what your runs are about — a repository, a work item, a pull
request. It is always a class, and the workflow names it:

```python
class Sweep(Workflow):
    subject = Repository
```

That declaration is what lets druks show the subject's life, and offer what may
be done to it, before any run about it exists. `start()`, `cancel()`, and
`Gate.answer()` hold you to it: a workflow that declares a subject is launched
with one of that class, and a workflow about nothing passes `subject=None`.

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
    def list_summaries(cls) -> list[SubjectSummary]:
        return [repository.get_summary() for repository in cls.list_open()]
```

Say which rows are on the board and druks has the rest: every subject already
answers with its id and its `label`, which is the one line it shows itself as.
Add a summary of your own only when the board should show more:

```python
from druks.workflows import SubjectSummary


class RepositorySummary(SubjectSummary):
    open_findings: int


class Repository(StoredSubject):
    def get_summary(self) -> RepositorySummary:
        return RepositorySummary.model_validate(self)
```

When you keep no row for it, subclass `Subject` instead — identity is all the
platform needs, and the id is the whole record, which is also its label:

```python
from druks.workflows import Subject


class PullRequest(Subject):
    @classmethod
    def list_summaries(cls) -> list[SubjectSummary]:
        return [pull_request.get_summary() for pull_request in cls.list_open()]
```

Any id names one of these, so a detail read always answers. Override
`get_for_subject_id()` to return None for a shape yours could never wear —
`owner/repo#7` is a pull request, `nonsense` is a 404.

Each subject a workflow declares must implement `list_summaries()`. The board
reads it. Druks checks this at load. If the method is missing, the extension
does not load. The error names the extension, the subject, and the method.

Druks serves the same `/api/night_watch/repository` surface either way: a board,
a page for one, and a live stream of either, mounted for every subject your
workflows declare. Each response pairs your summary with the run's status,
timeline, agent calls, artifacts, and the question it is waiting on. Override
`get_subject_activity()` on the extension only to add a passing detail of your
own, like "Building sandbox VM…".

Hand the subject itself to anything that asks for one — starting a run,
answering a gate, recording an event:

```python
await NightWatch.dispatch(subject=repository)
```

Inside the workflow, `self.subject` is resolved through the declared class —
live, not a snapshot taken at dispatch. A run that parks on a gate for three
days resumes against whatever the row says then, and finds nothing if it was
deleted meanwhile.

You name what happened to the row: a work item ships, gets cancelled. Whether a
run is working on it is druks's to say, and you read that off the status:

```python
status = repository.get_status()
if status.is_parked:
    ...  # a run stopped to ask a human something
```

`status.kind` names the workflow currently driving the row and `status.gate` the
question it stopped on. While a run is working, `await repository.get_phase()`
returns the step it is on.

## Record events and react to signals

Record an extension event through the extension so ownership is stamped:

```python
NightWatch.record_event(
    type="report.published",
    subject=repository,
    payload={"url": report_url},
)
```

`type` is the milestone's own word, which the feed reads as one — there is no
rendering hook to implement. Lifecycle events for subjected workflows are
recorded automatically. Call `record_event()` inside a platform-bound
transaction such as a request, durable step, or subscriber.

A feed row carries facts, never prose: its kind, the workflow it came from, the
subject identity, and the event's payload — a client words them. Give the subject
a ``label`` — the one line it shows itself as — and every event written about it
carries that from then on:

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

`subject=Repository` narrows to any workflow about a repository. Narrowing to
one workflow with `workflow=Sweep` says the same thing and more, because the
workflow declares its subject — so the two are never written together, and the
body is handed its subject either way.

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
extension namespace, tagged with the extension's name — a router declares only
the prefix its own resource is called:

```python
router = APIRouter(prefix="/reviews")
```

Your routes run authenticated: the loader mounts every router behind the
platform's identity gate — a Bearer PAT or the signed-in session — so nothing
anonymous reaches your code, and you never write auth yourself. Read the
caller when a route scopes by who is asking:

```python
from druks.accounts import current_account_id

@router.get("/reviews")
def list_reviews() -> list[ReviewResponse]:
    return Review.list_for_account(current_account_id.get())
```

Tagging a route `agent` also derives it into an MCP tool: you give it an explicit
`operation_id`, and Druks derives the tool name by prefixing it with your extension
name — write `operation_id="add_peer"` in `peer_tracker` and the tool is
`peer_tracker_add_peer`. The docstring is the description. `GET` derives read-only;
a write declares `x-destructive: false` or `x-idempotent: true` in `openapi_extra`
only when that statement is genuinely true, otherwise the safe defaults are
destructive and non-idempotent. Boot refuses a missing `operation_id` or a missing
docstring.

Two spellings run through druks, and which one a segment wears says who owns it:

| | |
| --- | --- |
| `snake_case` | an identity the platform serves — your extension name, a subject type |
| `kebab-case` | a resource you named — your route prefixes, your frontend paths |

So `/api/review/pull_request` is the board of review runs, keyed by subject, and
`/api/review/reviews` is the resource your own POST creates. The platform's two
segments — `<subject_type>` and `transcripts` — are matched before your routers,
so nothing you declare can take a read druks serves, not even a catch-all. Name a
router for its own resource and the question never comes up.

## Declare a service

A service identity is the appliance's own registered app at an external
provider — one per deployment, keyed by a service string; the platform's
GitHub App is the first one. OAuth grants are not service identities — the
platform stores those when the operator connects (see "Connect provider
accounts") — and a credential only your extension posts with belongs in your
extension settings instead.

Declare one class in `services.py` and the platform does the rest: it renders
the connect card in Settings (the heading derives from `name`), verifies and
stores the paste (`SecretStr` fields land encrypted, plain fields become
identity facts), and reports `druks doctor` state:

```python
from pydantic import BaseModel, Field, SecretStr

from druks.services import Service, ServiceConnectError


class Gmail(Service):
    name = "gmail"
    description = "The appliance's own OAuth client — every mailbox authenticates against it."

    class Settings(BaseModel):
        client_id: str = Field(title="Client ID")
        client_secret: SecretStr = Field(title="Client secret")
```

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

Set `required = False` on the class when the appliance is healthy without the
service connected; doctor then notes it instead of reporting pending setup.

Key the service for the integration your extension consumes (`"gmail"`), not
the provider (`"google"`). A second integration on the same provider declares
its own service, and the operator decides per card whether the underlying
registration is shared or a narrower one — that choice is their scope and
blast-radius control.

## Connect provider accounts (OAuth)

Declare the OAuth endpoints on the service that holds the client
credentials. The `Settings` model must have `client_id` and `client_secret`
fields:

```python
class Acme(Service):
    name = "acme"
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

`extra_authorize_params` declares the provider's consent-query quirks; the
platform adds them to every sign-in it starts for the service. The example
shows Google's: it grants a refresh token only when the consent asks for
`access_type=offline` with `prompt=consent`.

`identity_endpoint` names the provider endpoint that returns the signed-in
account's facts (email, username, name). Druks calls it once at consent and
shows the facts as the connection's label in Settings. `identity_scopes`
are the scopes that call needs; they join the consent ask.

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
declarations through an abstract base. Set `abstract = True`: the base never
registers, and each subclass inherits everything it declares, `Settings`
included:

```python
class GoogleOauth(Service):
    abstract = True
    authorization_endpoint = "https://accounts.google.com/o/oauth2/v2/auth"
    token_endpoint = "https://oauth2.googleapis.com/token"
    extra_authorize_params = {"access_type": "offline", "prompt": "consent"}
    identity_endpoint = "https://openidconnect.googleapis.com/v1/userinfo"
    identity_scopes = ("openid", "email")

    class Settings(BaseModel):
        client_id: str = Field(title="Client ID")
        client_secret: SecretStr = Field(title="Client secret")


class Gmail(GoogleOauth):
    name = "gmail"


class Calendar(GoogleOauth):
    name = "google_calendar"
```

Declare your extension's use of the service, with the scopes your calls
need:

```python
class NightWatch(Extension):
    name = "night_watch"
    acme = Acme.with_scopes("profile.read", "posts.write")
```

A *connection* is one signed-in provider account. A user can hold many per
provider — one per mailbox, handle, or workspace — and the platform stores
each one: the refresh token, the granted scopes, the owner. Your workflow
code reads them through the declaration and gets a token per connection:

```python
for connection in NightWatch.acme.list_for_account(account_id):
    token = await connection.get_access_token()
```

`account_id` is the caller: `self.account_id` in a run body,
`current_account_id.get()` in a route, the handler's argument in a
subscriber. `NightWatch.acme.get(connection_id)` returns one connection
when your own row stored its id. Each connection carries `id`, `scopes`, `identity` — the
provider's facts for the sign-in — `account_id` — the druks account that
signed it in — and `connected_at`. The handle serves
live connections only. A revoked connection drops out of `get` and
`list_for_account`, but its platform row survives with its owner and
identity. Your rows never need tombstone copies of either.

Your UI starts a sign-in by opening `/api/oauth/acme/connect` — the
platform runs the consent with the union of every installed extension's
declared scopes and stores the connection for the signed-in user. A fresh
sign-in always creates a new connection, even for a provider account that
was connected before. To widen an existing connection's scopes, open
`/api/oauth/acme/connect?connection=<id>`; reconsent replaces its tokens.
Reconsent names the row, so it also returns a revoked connection to life
under its old id — the only way a row comes back.
Add `?next=/app/night_watch/accounts` to land the user back on your
page after consent instead of the generic "connected" page. `next`
must be a bare path starting with `/` — a URL with a scheme or host is
rejected, so the door can never redirect off the box. Register
`https://<host>/api/oauth/callback` as the redirect URI at the provider; it
serves every service.

React to sign-ins with the signal machinery. The platform publishes
`oauth.connected` when a consent completes. `reconsent` is true when it
replaced an existing connection's tokens, including a revoked row that
returned to life. It publishes `oauth.disconnected` when a connection is
revoked — by the user, or because the service's client credentials were
replaced. Revocation is a state, not a deletion: your subscriber can still
read the connection it is told about. Subscribe in `subscribers.py`:

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

`get_access_token(scopes=("profile.read",))` asks the provider for a token
narrower than the grant — pass it when the token goes to untrusted compute,
with a subset of the connection's scopes. `cached=False` refreshes past the
cache for a full-lifetime token.

## Extension settings and checks

An inner `ExtensionSettings` class defines dashboard-editable knobs and owns their
cross-field coherence:

```python
from typing import Literal

from pydantic import Field

from druks.extensions import Extension, ExtensionSettings, Secret


class NightWatch(Extension):
    name = "night_watch"

    class Settings(ExtensionSettings):
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
`SecretStr`, including optional forms. Nested Pydantic models are rejected.
Secret values and submitted validation errors are redacted. Declare a secret
field as `Secret`: an unset one is an empty, falsy `SecretStr`, so
`if self.service_token:` reads set-ness and `.get_secret_value()` never needs
a guard. A secret whose pasted value carries meaningful newlines — a PEM
private key — may declare `json_schema_extra={"multiline": True}`: the
settings form renders a textarea and the paste keeps its newlines; storage,
redaction, and write-only semantics are unchanged. `section` is a plain
heading rendered in first-declaration order, with
unsectioned fields first. `visible_when` takes one same-model `{field: value}`
equality condition. Its controller must be non-secret and unconditional, and a
`Literal` controller requires one of its declared members.
Hidden fields keep their stored values. Read the resolved model with
`NightWatch.settings()`. The settings form runs `clean()` against the
resolved settings after the proposed edits and rejects an incoherent save. `druks doctor`
runs the same method over stored settings so rows from older releases or manual database
edits remain visible. Workflow settings stay plain Pydantic `BaseModel` declarations.

An extension may contribute precondition checks beyond settings coherence through
`checks`. Return `druks.doctor.CheckResult`; Druks namespaces the result and converts
a raising or malformed check into a failure without hiding later checks.

## Test an extension

Installing Druks registers its pytest plugin. An extension can request the
fixtures directly without a `conftest.py` or `pytest_plugins` declaration:

| Fixture | Contract |
| --- | --- |
| `druks_db` | A SQLAlchemy `Session` bound to a per-test transaction. Commits become savepoints, and teardown rolls the outer transaction back. |
| `druks_client` | An authenticated `TestClient` with installed extensions mounted, sharing `druks_db`'s connection. |
| `druks_redis` | The test Redis database, flushed before the test. |
| `druks_without_dispatch` | Workflow starts and run-phase writes become no-ops, for tests that stand up no durable engine. |
| `druks_without_remote_config` | Every `.druks` namespace lookup misses, so prompts resolve to bundled templates and config to its declared defaults. |

The fixtures are not autouse. A test that requests `druks_client` also gets
`druks_db`; Redis is touched only when a test requests `druks_redis`.

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

`seed_run` writes both the run row and the DBOS workflow status from which
`Run.state` is derived. Its `kind` is required. Pass `input_gate` when seeding
`state="pending_input"`. `seed_call` accepts an `Agent` or its string id.

`make_settings(tmp_path, **overrides)` builds isolated application settings.
`configure_app_for_test(settings=..., authenticated=False)` returns the mounted
app when a test needs its own client or an unauthenticated request path;
`druks_client` covers the normal authenticated case.

The fixtures never read the application's settings. They read
`DRUKS_TEST_DATABASE_URL` and `DRUKS_TEST_REDIS_URL`, defaulting to a local
`druks_test` database and Redis index 15, and they point the code under test at
the same pair — so a run cannot reach whatever `DRUKS_DATABASE_URL` and
`DRUKS_REDIS_URL` name. Create the database once (`createdb druks_test`); the dev
Compose project already does.

On that database the plugin creates `citext`, imports installed extension models,
runs SQLAlchemy `create_all`, seeds platform reference rows, and builds the DBOS
system tables through DBOS's database migrations. It never resets or drops a
schema; per-test writes made through `druks_db` are rolled back. `druks_redis`
runs `FLUSHDB` on the test index.

## Frontends

An installed extension is visible in the dashboard without shipping any UI. The
shell reads the installed roster from `/api/extensions` and gives every
extension an entry in the app switcher plus generic pages: a board per subject
type, and a subject page with the run timeline, transcripts, and gate controls.
There is nothing to declare — the subject summary's fields are the board row.
The switcher label is derived from `name` (underscores become spaces).

Chrome contributions are declared data. `navigation` on the extension class
adds appbar subnav tabs as `(url, name)` pairs, rendered by the shell for
generic pages and shipped frontends alike; the active tab is the one whose
url is the longest prefix of the current location:

```python
class NightWatch(Extension):
    name = "night_watch"
    navigation = [("/night_watch", "reports")]
```

To go beyond the generic pages, ship a frontend: an ES module the shell mounts
inside its own document, below the chrome. The scaffold ships a placeholder
`druks_night_watch/dist/entry.js`; point your frontend build's output at that
`dist/` directory to replace it. The contract (`shellApi: 1`):

- `entry.js` exports `shellApi = 1` and `mount(el, ctx)`, which renders into
  `el` and returns a dispose function. A missing `mount` or a version mismatch
  renders a visible error panel in the shell.
- `ctx` carries `apiBase` (`/api/<name>`), `navigate(path)` for shell-side
  navigation, `theme.accent`, and `markdown(source)` — the shell's own
  markdown renderer, so an app doesn't bundle one. The app renders in the
  shell's document, so the shell's CSS variables cascade into it. The shell
  re-broadcasts every location change as a `popstate` event while the app is
  mounted; route by reading `location.pathname` under `/<name>/`.
- `dist/style.css`, when present, is loaded while the app is mounted.
- Build the bundle with `react`, `react-dom`, `react-dom/client`, and
  `react/jsx-runtime` externalized (Vite library mode); the shell's import map
  resolves them to its own copy, so one React instance serves the whole
  document. Other dependencies are bundled as usual.

The bundled Druks SPA also has a shared React extension registry. Joining that
shell requires compiling the extension's UI module into the dashboard image;
installing a Python wheel cannot mutate an existing JavaScript bundle. See the
[frontend guide](../frontend/README.md) for that in-repository path.

## Stable author imports

Import from concern namespaces, not from `druks.durable` or internal modules:

| Namespace | Public names |
| --- | --- |
| `druks.accounts` | `current_account_id` |
| `druks.extensions` | `Extension`, `ExtensionSettings`, `Secret` |
| `druks.services` | `Service`, `ServiceConnectError`, `ServiceNotConnectedError`, `OauthClient`, `OauthExchangeError`, `OauthRefreshError` |
| `druks.secrets.fields` | `EncryptedJsonField`, `SecretsMapping` |
| `druks.agents` | `Agent`, `AgentOutput` |
| `druks.workflows` | `Workflow`, `Gate`, `step`, run/agent response types, lifecycle enums and workflow errors |
| `druks.db` | `Base`, `StoredSubject`, `db_session` |
| `druks.schemas` | `BaseResponse` |
| `druks.signals` | `subscribe` |
| `druks.events` | `Event` |
| `druks.prompts` | `render_prompt` |
| `druks.webhooks` | `Webhook`, `verify_hmac_sha256` |
| `druks.testing` | `run_workflow`, `seed_run`, `seed_call`, `init_db` — plus the fixtures the plugin registers |

The root `druks` package deliberately exports only its version.
