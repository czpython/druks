# AGENTS.md

Druks runs durable agent apps on DBOS and Postgres. It owns workflow execution,
persisted state, events, gates, webhooks, sandbox access, and the shared
dashboard. Apps are standalone Python packages. They register through the
`druks.apps` entry point. `software_factory` is the bundled reference app for
coding work through GitHub pull requests.

## Read map

Start with `README.md`, then read only the material relevant to the task:

- Workflow lifecycle, state, replay, or recovery: `docs/concepts.md`
- App contracts or the public author surface: `docs/writing-an-app.md`
- Configuration or environment variables: `docs/configuration.md`
- Local install and operations: `docs/full-local.md`
- Remote deployment: `docs/deployment.md`
- Failure diagnosis: `docs/troubleshooting.md`
- Backend contribution, migrations, or verification: `docs/development.md`
- Migration head: `alembic heads`, not a scan of `backend/migrations/versions/`
- Shared SPA work: `frontend/README.md`
- Documentation navigation and audience ownership: `docs/index.md`
- The checklist and craft gate every change is held to: `.druks/review/checklist.md`.

For app-surface changes, inspect the proof app at
`backend/tests/druks-field_notes/` and its tests as well as the author guide.

## Architectural boundaries

- Keep platform and app ownership explicit. Keep GitHub issue, branch, PR, and
  coding-agent policy in `software_factory`, not in Druks core.
- Describe durability precisely. Druks reuses completed durable checkpoints
  during replay. An interrupted operation can run again. Do not imply
  arbitrary-line resume or exactly-once external side effects.
- Derive `Run.state` from DBOS workflow status. Do not add a second writable state mirror.
- Import the public concern namespaces documented in `docs/writing-an-app.md`.
  Do not import Druks internals.
- Keep backend app discovery in runtime packaging. Keep shared-dashboard app UI
  registration in the frontend build. Keep standalone `dist/` delivery separate.
- Keep generic agent, harness, workspace, sandbox, event, gate, webhook, and
  settings plumbing in Druks. Keep domain-specific policy in the app.
- Grow the author surface by parameter, not by namespace. If the SDK lacks a
  capability, widen the primitive that owns it. Add a keyword argument or a
  method to the class that holds the data. Do not add a namespace, facade,
  context object, or one-use helper module.
- Do not import an app from an author-surface module. Import only
  `druks.workflows`, `druks.agents`, `druks.events`, `druks.signals`, `druks.db`,
  `druks.schemas`, `druks.prompts`, `druks.durable`, `druks.apps`, and
  `druks.webhooks`. A reference to `druks.build` or another app in these modules
  inverts the platform.
- Derive liveness from run state. Do not mirror it in a column. Store an external
  outcome after its owner announces it. Do not infer that outcome from run
  lifecycle. A merge can occur after Druks stops the related run.
- Use `@step` to mark a replay checkpoint, not an expensive call. On replay the body
  re-executes from the top and completed steps return cached results, so code outside a
  step runs again. Moving the boundary changes correctness, not performance.
- Store transient live state and mutual exclusion in Redis, keyed by run id.
  Do not give either one a durable column, row lock, or in-process lock.
- Do not add a store before something reads it.
- Route a workflow cadence or pause through its schedule overrides. Do not use a
  settings column for this purpose.
- Use one canonical name and shape for each contract. Fail loudly on every other shape.
- Do not type-switch over a typed stream in app code. When a projection needs
  ordering or anchoring, grow the SDK primitive instead of an `isinstance` chain.
- Put only identity and facts in a read-side field. Keep UI wording in the app pages.
- Give a shared resource one global registry. When a second
  consumer needs a different answer, add a scoping axis. Do not add one in anticipation.

## Layout

- **Backend:** `backend/druks/` contains FastAPI, DBOS, SQLAlchemy 2.0,
  Pydantic v2, and bundled apps.
- **Migrations:** `backend/migrations/` contains platform Alembic migrations.
- **Tests:** `backend/tests/` contains the pytest suite backed by real Postgres.
- **Proof app:** `backend/tests/druks-field_notes/` contains the independently
  packaged proof app.
- **Frontend:** `frontend/` contains the React 19 and Vite shared SPA. The
  backend image includes its repository-root `dist/` output.
- **Deployment:** `deploy/` contains Compose files, Caddy configuration, and
  sandbox image inputs. The public runbook is `docs/deployment.md`.
- **Documentation:** `docs/` contains the public and contributor guides.
- **CI:** `.github/workflows/` contains PR checks and the release-image build.

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
- Make sure that behavioral claims match current source and focused tests. Distinguish
  framework capabilities from `software_factory` behavior and guarantees from policy.
- If contributor routing, repository structure, commands, or an architectural
  invariant changes, update this file.

## Style

- Make the minimum change that solves the problem. No speculative abstractions,
  configurability, or error handling for impossible cases. Each changed line
  must trace to the request. Do not improve adjacent code.
- Comments explain a non-obvious *why*—a constraint, invariant, or workaround—not
  what the next line does. Do not add section-divider banner comments.
- If the signature and body do not make a contract obvious, add a class, module,
  or function docstring.
- Exception classes live in the package's `exceptions.py`, not in contracts or
  models.
- Keep forward-looking notes in the issue tracker, not in source comments.
