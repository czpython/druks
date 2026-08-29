# AGENTS.md

Druks runs durable agent apps on DBOS and Postgres. It owns
workflow execution, persisted state and events, gates, webhooks, sandbox access,
and the shared dashboard. Apps are standalone Python packages
that self-register through the `druks.apps` entry point. `software_factory` is the
bundled reference app for coordinating coding agents through GitHub PRs.

## Read map

Start with `README.md`, then read only the material relevant to the task:

- Workflow lifecycle, state, replay, or recovery: `docs/concepts.md`.
- App contracts or the public author surface: `docs/writing-an-app.md`.
- Configuration or environment variables: `docs/configuration.md`.
- Local install and operations: `docs/full-local.md`.
- Remote deployment: `deploy/README.md`.
- Failure diagnosis: `docs/troubleshooting.md`.
- Backend contribution, migrations, or verification: `docs/development.md`.
- The current migration head is `alembic heads` — not a scan of `backend/migrations/versions/`.
- Shared SPA work: `frontend/README.md`.
- Documentation navigation and audience ownership: `docs/index.md`.
- The checklist and craft gate every change is held to: `.druks/review/checklist.md`.

For app-surface changes, inspect the proof app at
`backend/tests/druks-field_notes/` and its tests as well as the author guide.

## Architectural boundaries

- Keep platform and app ownership explicit. GitHub issue, branch, PR, and
  coding-agent policy belongs to `software_factory`, not to Druks core.
- Describe durability precisely: completed durable checkpoints are reused when
  orchestration replays, but an interrupted operation may run again. Do not imply
  arbitrary-line resume or exactly-once external side effects.
- `Run.state` is derived from DBOS workflow status. Do not add a second writable
  state mirror.
- App authors import the public concern namespaces documented in
  `docs/writing-an-app.md`, not Druks internals.
- Backend app discovery is runtime packaging. Shared-dashboard app UI
  registration is a compile-time frontend concern. Standalone apps may ship
  their own `dist/`; do not conflate the two delivery paths.
- Druks owns generic agent, harness, workspace, sandbox, event, gate, webhook, and
  settings plumbing. Domain-specific policy stays in the app.
- The author surface grows by parameter, not by namespace. When an app needs
  something the SDK lacks, widen the primitive that already owns the concern — a keyword
  argument, a method on the class holding the data. Do not add a namespace, a facade, a
  context object, or a helper module whose only justification is that the call site
  would read shorter.
- No author-surface module imports an app. `druks.workflows`, `druks.agents`,
  `druks.events`, `druks.signals`, `druks.db`, `druks.schemas`, `druks.prompts`,
  `druks.durable`, `druks.apps`, and `druks.webhooks` are what an author imports;
  a reference to `druks.build` or any other app inside them inverts the platform.
- Liveness — is this subject still being worked — derives from run state; never mirror
  it in a column. An outcome somebody else owns, such as whether a pull request was
  merged, is stored when its owner announces it — never inferred from run lifecycle,
  which cannot see a merge that happens after druks has stopped.
- `@step` marks a replay checkpoint, not an expensive call. On replay the body
  re-executes from the top and completed steps return cached results, so code outside a
  step runs again. Moving the boundary changes correctness, not performance.
- Transient live state and mutual exclusion live in Redis, keyed by run id. Neither
  gets a durable column, a row lock, or an in-process lock.
- Do not add a store before something reads it, and route a workflow's cadence or pause
  through its schedule overrides rather than a settings column.
- A contract is one canonical name and shape that fails loudly on anything else. Do not
  accept two spellings of the same thing.
- App code does not type-switch over a typed stream. When a projection needs
  ordering or anchoring, grow the SDK primitive instead of an `isinstance` chain.
- A read-side field carries identity and facts — gate name, kind, reason code. UI
  wording lives in the app's own pages, never on the wire.
- A shared resource gets one global registry delivered everywhere. Add a scoping axis
  when a second consumer needs a different answer, not in anticipation.

## Layout

- `backend/druks/` — FastAPI, DBOS, SQLAlchemy 2.0, Pydantic v2, and bundled
  apps.
- `backend/migrations/` — platform Alembic migrations.
- `backend/tests/` — pytest suite backed by real Postgres.
- `backend/tests/druks-field_notes/` — independently packaged proof app.
- `frontend/` — React 19 and Vite shared SPA; production output is repository-root
  `dist/` and is copied into the backend image.
- `deploy/` — Compose files, the bind-mounted Caddy configuration, and sandbox
  image inputs.
- `docs/` — public concepts, configuration, author, operator, troubleshooting, and
  contributor guides.
- `.github/workflows/` — PR checks and release image build.

## Verification

Backend tests need Postgres on `localhost:5432` with user and password `druks`, and
the `druks_test` database. `DRUKS_TEST_DATABASE_URL` overrides it —
`DRUKS_DATABASE_URL` is the runtime's and the suite never reads it. DBOS
integration tests also read `DRUKS_TEST_PG`. Start the development database
with:

```bash
docker compose -f deploy/compose.dev.yaml up -d
```

Run the backend gates:

```bash
uv run ruff check backend
uv run ruff format --check backend
uv pip install -e backend/tests/druks-field_notes
uv run pytest backend/
```

The suite collects the proof app, so `pytest backend/` fails at collection
until that editable install has run.

If the public app surface changed, also install and exercise the proof
app as described in `docs/development.md`.

Run the frontend gates:

```bash
npm --prefix frontend run lint
npm --prefix frontend test
npm --prefix frontend run build
```

The PR workflows in `.github/workflows/on-pull-request-*.yml` are the source of
truth for CI, including the proof-app install phase.

## Documentation discipline

- Put product behavior, setup, operations, troubleshooting, and app author
  contracts in the appropriate public guide. Keep this file limited to task
  routing, architectural boundaries, and contributor rules.
- Link to one canonical explanation instead of copying it into multiple pages.
- Verify behavioral claims against current source and focused tests. Distinguish
  framework capabilities from `software_factory` behavior and guarantees from policy.
- Update this file only when contributor routing, repository structure, commands,
  or a load-bearing architectural invariant changes.

## Style

- Make the minimum change that solves the problem. No speculative abstractions,
  configurability, or error handling for impossible cases. Every changed line
  should trace to the request; do not improve adjacent code.
- Comments explain a non-obvious *why*—a constraint, invariant, or workaround—not
  what the next line does. Do not add section-divider banner comments.
- Add class, module, or function docstrings only when the
  signature and body do not already make the contract obvious.
- Exception classes live in the package's `exceptions.py`, not in contracts or
  models.
- Keep forward-looking notes in the issue tracker, not in source comments.
