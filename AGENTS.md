# AGENTS.md

Druks runs durable agent applications on DBOS and Postgres. It owns
workflow execution, persisted state and events, gates, webhooks, sandbox access,
and the shared dashboard. Apps are **extensions**: standalone Python packages
that self-register through the `druks.extensions` entry point. `build` is the
bundled reference extension for coordinating coding agents through GitHub PRs.

## Read map

Start with `README.md`, then read only the material relevant to the task:

- Workflow lifecycle, state, replay, or recovery: `docs/concepts.md`.
- Extension contracts or the public author surface: `docs/writing-an-extension.md`.
- Configuration or environment variables: `docs/configuration.md`.
- Local install and operations: `docs/full-local.md`.
- Remote deployment: `deploy/README.md`.
- Failure diagnosis: `docs/troubleshooting.md`.
- Backend contribution, migrations, or verification: `docs/development.md`.
- Shared SPA work: `frontend/README.md`.
- Documentation navigation and audience ownership: `docs/index.md`.

For extension-surface changes, inspect the proof extension at
`backend/tests/druks-field_notes/` and its tests as well as the author guide.

## Architectural boundaries

- Keep platform and extension ownership explicit. GitHub issue, branch, PR, and
  coding-agent policy belongs to `build`, not to Druks core.
- Describe durability precisely: completed durable checkpoints are reused when
  orchestration replays, but an interrupted operation may run again. Do not imply
  arbitrary-line resume or exactly-once external side effects.
- `Run.state` is derived from DBOS workflow status. Do not add a second writable
  state mirror.
- Extension authors import the public concern namespaces documented in
  `docs/writing-an-extension.md`, not Druks internals.
- Backend extension discovery is runtime packaging. Shared-dashboard extension UI
  registration is a compile-time frontend concern. Standalone extensions may ship
  their own `dist/`; do not conflate the two delivery paths.
- Druks owns generic agent, harness, workspace, sandbox, event, gate, webhook, and
  settings plumbing. Domain-specific policy stays in the extension.

## Layout

- `backend/druks/` — FastAPI, DBOS, SQLAlchemy 2.0, Pydantic v2, and bundled
  extensions.
- `backend/migrations/` — platform Alembic migrations.
- `backend/tests/` — pytest suite backed by real Postgres.
- `backend/tests/druks-field_notes/` — independently packaged proof extension.
- `frontend/` — React 19 and Vite shared SPA; production output is repository-root
  `dist/` and is copied into the backend image.
- `deploy/` — Compose files, the bind-mounted Caddy configuration, and sandbox
  image inputs.
- `docs/` — public concepts, configuration, author, operator, troubleshooting, and
  contributor guides.
- `.github/workflows/` — PR checks and release image build.

## Verification

Backend tests need Postgres on `localhost:5432`, with user, password, and database
`druks` by default. `DRUKS_DATABASE_URL` overrides the application/test database;
DBOS integration tests also read `DRUKS_TEST_PG`. Start the development database
with:

```bash
docker compose -f deploy/compose.dev.yaml up -d
```

Run the backend gates:

```bash
uv run ruff check backend
uv run ruff format --check backend
uv run pytest backend/
```

If the public extension surface changed, also install and exercise the proof
extension as described in `docs/development.md`.

Run the frontend gates:

```bash
npm --prefix frontend run lint
npm --prefix frontend test
npm --prefix frontend run build
```

The PR workflows in `.github/workflows/on-pull-request-*.yml` are the source of
truth for CI, including the proof-extension install phase.

## Documentation discipline

- Put product behavior, setup, operations, troubleshooting, and extension author
  contracts in the appropriate public guide. Keep this file limited to task
  routing, architectural boundaries, and contributor rules.
- Link to one canonical explanation instead of copying it into multiple pages.
- Verify behavioral claims against current source and focused tests. Distinguish
  framework capabilities from `build` behavior and guarantees from policy.
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
