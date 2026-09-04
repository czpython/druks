---
title: "Concepts and guarantees"
description: "Understand Druks ownership boundaries, durable recovery, gates, agents, events, and access topology."
icon: "blocks"
---

## The problem Druks solves

Agent apps routinely cross boundaries that do not fit a request handler. They
call slow models, provision machines, wait for people, and react to webhooks.
They can also outlive a process or deployment. A retry of the full script costs
time and can cause a side effect again. One long-lived process does not provide
recovery.

Druks separates durable control flow from app code. DBOS records workflow
progress in Postgres. Druks layers workflows, agents, gates, subjects, events,
settings, and app loading on top, then exposes their state through an API and
dashboard.

## The app boundary

An app is an independently packaged Python distribution installed into the same
environment as Druks. Its package registers an `App` subclass:

```toml
[project.entry-points."druks.apps"]
night_watch = "druks_night_watch.app:NightWatch"
```

At boot Druks resolves installed entry points, imports each app's models
and role modules, and mounts its routes under `/api/<name>`. The entry-point
name must match `App.name`. The same name scopes:

- The API namespace
- The default `<name>_` table prefix
- The app's Alembic version table
- App setting keys.

### Druks owns

- Durable execution, queues, schedules, run state, cancellation, and gates
- Agent descriptors, harness dispatch, and sandbox access
- Subject timelines, the event feed, signals, webhook dispatch, and notifications
- MCP and skill delivery, settings, MCP secret encryption, and diagnostics
- The FastAPI server, shared dashboard shell, and app loading.

### An app owns

- Domain workflows, their subjects, and their start policy
- Agents, prompts, and structured output contracts
- Domain models, migrations, HTTP routes, and subject summaries
- Normalized event reactions and provider-specific webhook behavior
- Provider credentials and prerequisites that are specific to the domain
- Optional static frontend assets in the app package.

The bundled `software_factory` app owns projects, work items, ticket intake,
GitHub branches, pull requests, coding-agent policy, and dashboard pages. These
features are examples, not platform guarantees.

## Durability and recovery

A workflow defines either:

- **Single-step:** `run()` is one durable operation.
- **Multistep:** `run_multistep()` provides replayable orchestration across explicit `@step`
  operations, agent calls, and gates.

Each completed durable operation has a recorded result. On recovery, DBOS
re-enters the workflow and returns those recorded results at the same operation
boundaries. This has several consequences:

- Druks reuses completed checkpoints. An agent call uses its checkpoint
  or the checkpoint of the enclosing step.
- Ordinary orchestration code can run again to rebuild in-memory decisions.
- Plain instance attributes are working memory, not a separate persisted object.
- Code that stops inside a step can run again.
- External side effects inside a step require stable idempotency keys.
- A workflow structure change can affect active runs. Treat the change as a
  deployment compatibility decision.

Druks does not promise to preserve a live external agent process through a
worker crash. Agent execution is a durable operation around a process in a
sandbox. Recovery follows the operation boundary in this section.

## When Druks fits

Druks is for apps whose work crosses process lifetimes. This work can include
durable operations, isolated agent calls, external triggers, and waits. It is
also useful for independent app packages that share one operating substrate.

It is not an agent model SDK, a sandbox provider, or a reason to wrap a
single short model call in a workflow. Drukbox owns host provisioning, and an
app still owns domain policy and side-effect idempotency.

### State has one lifecycle owner

The `durable_runs` row stores the Druks-owned facts DBOS has no slot for: the
current gate ask, the failure text, and timestamps. The run's subject lives on
the DBOS workflow itself as custom attributes. `workflow_status` alone answers
"runs for this subject." The run row does not store the app —
it is workflow-class metadata, derivable from the run's `kind` through the
app registry. The API reads the row's lifecycle state from DBOS workflow status:

```text
scheduled -> running -> finished
                    \-> parked -> running
                    \-> failed
                    \-> cancelled
```

A run whose DBOS status row is missing reads `scheduled` during the short
enqueue window and `orphaned` after five minutes. `orphaned` is terminal: the
workflow record needed to execute it no longer exists.

Subjected workflow starts use DBOS queue deduplication per workflow kind and
subject. A duplicate start returns the active run's id. Druks does not impose
that policy on subjectless background runs.

## Waiting for people and systems

A `Gate` defines a typed reply and a durable receive topic. When a workflow
waits at a gate, Druks:

1. Releases each warm sandbox that the workflow holds.
2. Records `parked` and the request for the operator.
3. Sends an optional notification.
4. Suspends the workflow until a reply arrives or the 14-day timeout expires.
5. Clears the gate and returns the validated reply after the workflow resumes.

Each parked round accepts one answer through an idempotency key. In-app review
requires a subject because the subject read-side is where the question appears.
A subjectless custom gate must override `on_wait()` to send an external
notification. Without this override, the gate fails instead of creating an
invisible wait.

Cancellation clears the outstanding ask and asks DBOS to cancel the workflow.
A parked subject then releases its deduplication slot so another run can start.

## Agents, harnesses, workspaces, and sandboxes

These terms describe different ownership layers:

| Layer | Responsibility |
| --- | --- |
| Agent | App-owned prompt, output contract, and run timeout |
| Harness | Platform adapter that invokes an agent CLI for a model |
| Workspace | App customization of what a call receives, such as a cloned repository |
| Sandbox | Drukbox-provisioned isolated host where the harness process runs |
| Provider | A Drukbox backend name that supplies the host. `docker` and `exe` select install shapes |

Each agent call validates a strict Pydantic output contract. It records model
and cost metadata. It also stores the transcript, stderr, prompt, output, and a
secret-free capability manifest. The model choice determines the harness. The
configured Drukbox service determines the sandbox provider. Workflow authors do
not write provider-specific execution code.

By default, each agent call uses an ephemeral sandbox. A workflow can retain one
warm sandbox across a segment. Druks releases it before a gate and at workflow
exit. Druks also rotates it before the lease becomes too short for another
call. Store durable state in an external system such as Git, not only on the VM.

## Events, signals, webhooks, and subjects

A subject is the object of a run. It is always a class that represents an app
row or an identity. The workflow declares the class. Thus, Druks knows the
subject kind before a run exists. Only the subject type and ID travel with the
run. A run can outlive its subject row.

Each run action enters an append-only
event log. Apps add domain events and a summary for each subject class. Druks
supplies pagination, activity composition, and a live fact feed. The client
owns the words that describe a subject.

Signals connect producers to app reactions. The publisher waits for their
delivery. Delivery occurs at least one time. A webhook error tells the provider
to send the webhook again. Durable lifecycle publishers also retry. Thus,
subscribers must be idempotent.

Webhook classes authenticate and normalize provider deliveries before
publishing signals. The framework supplies routing and deduplication. An
app or integration owns the provider payload and domain reaction.

## Settings and capabilities delivered to agents

Configuration has two planes:

- **Deployment:** `druks.toml` configures the deployment and creates the process environment.
- Postgres-backed settings configure the operator profile, harness defaults,
  app/workflow knobs, per-agent overrides, notifications, MCP servers,
  and skills.

Druks encrypts stored MCP tokens and OAuth grants at rest. It decrypts them
only to mint or deliver a token to an agent call. API responses and
capability manifests expose presence, never secret values.

Harness subscription payloads and notification webhook URLs do not use that
encryption envelope. They are standard Postgres fields. The API withholds or
masks their values. Thus, database and backup access is credential access.

Druks injects enabled MCP servers through the selected harness. A call receives
the enabled skills it requests, or every enabled skill when it requests none.
A workspace can also require an MCP server and supply its credentials.
Each agent call records its declarations and delivery so later evaluation can
distinguish capability sets without storing the tokens.

## Process and access topology

The shipped `web` process serves FastAPI, the SPA, DBOS workflows, and schedules.
Postgres stores app and DBOS state. Redis stores short-lived
coordination such as webhook deduplication, OAuth caches, and the sandbox
provisioning gate. Drukbox provisions sandbox hosts. Druks connects to them over
SSH.

Druks does not authenticate browsers. It resolves identity for each request. A
personal access token in `Authorization: Bearer` has first priority. If this
header is present, it must authenticate.

The configured `DRUKS_AUTH_MODE` handles requests without a token. In `header`
mode, the edge authenticates the operator. The edge can be exe.dev, Teleport,
or Cloudflare Access. It puts the operator email in the trusted identity
header. Druks maps this email to an account. The edge controls access, so this
mode permits account creation at first access.

In `none` mode, Druks has no authentication and exactly one operator account.
The first completed provider connection creates this account. Public
`/_external` routes stay outside the identity gate. These routes include
webhooks and the token-authenticated notification response.

The PAT-authenticated
`/mcp` endpoint also stays outside the gate. Each route keeps its own
authentication. A harness connection adds a capability to the current
account. It is not a login. See
[configuration](configuration.md#public-urls-and-access-control) for the
trust requirements. The edge must remove client-supplied copies of the identity
header.
