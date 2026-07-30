# AGENTS.md

Druks runs durable agent applications on DBOS and Postgres. It owns
workflow execution, persisted state and events, gates, webhooks, sandbox access,
and the shared dashboard. Apps are **extensions**: standalone Python packages
that self-register through the `druks.extensions` entry point. `ship` is the
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
- The current migration head is `alembic heads` — not a scan of `backend/migrations/versions/`.
- Shared SPA work: `frontend/README.md`.
- Documentation navigation and audience ownership: `docs/index.md`.

For extension-surface changes, inspect the proof extension at
`backend/tests/druks-field_notes/` and its tests as well as the author guide.

## Architectural boundaries

- Keep platform and extension ownership explicit. GitHub issue, branch, PR, and
  coding-agent policy belongs to `ship`, not to Druks core.
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
- The author surface grows by parameter, not by namespace. When an extension needs
  something the SDK lacks, widen the primitive that already owns the concern — a keyword
  argument, a method on the class holding the data. Do not add a namespace, a facade, a
  context object, or a helper module whose only justification is that the call site
  would read shorter.
- No author-surface module imports an extension. `druks.workflows`, `druks.agents`,
  `druks.events`, `druks.signals`, `druks.db`, `druks.schemas`, `druks.prompts`,
  `druks.durable`, `druks.extensions`, and `druks.webhooks` are what an author imports;
  a reference to `druks.build` or any other extension inside them inverts the platform.
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
- Extension code does not type-switch over a typed stream. When a projection needs
  ordering or anchoring, grow the SDK primitive instead of an `isinstance` chain.
- A read-side field carries identity and facts — gate name, kind, reason code. UI
  wording lives in the extension's own pages, never on the wire.
- A shared resource gets one global registry delivered everywhere. Add a scoping axis
  when a second consumer needs a different answer, not in anticipation.

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

Backend tests need Postgres on `localhost:5432` with user and password `druks`, and
the `druks_test` database. `DRUKS_TEST_DATABASE_URL` overrides it —
`DRUKS_DATABASE_URL` is the application's and the suite never reads it. DBOS
integration tests also read `DRUKS_TEST_PG`. Start the development database
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
  framework capabilities from `ship` behavior and guarantees from policy.
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

## DRUKS checklist

Run this against **every file the change touches, end to end — not just the diff**.
A pre-existing smell in an unchanged line is yours the moment you touch the file.

1. bare `return`, never `return None` (None only when it's a meaningful Optional
   value)
2. truthiness, never `is (not) None` (spell it out only when `0`/`""`/`{}` are real
   distinct values)
3. no guards for values our own system produced — read them directly; declarative
   subscribe-filters over body guards (guards on external/client data at a trust
   boundary are fine)
4. no data models except agent-output contracts; dicts/args/rows everywhere else
5. no comments about old behavior or adjacent code; end-state why-comments only,
   sparse
6. spelled-out names (workflow not wf); no one-caller abstractions
7. no platform plumbing in app code: `Artifact.add` / `Run.get` / `record_event` /
   `set_status`
8. druks reacts, never stamps — owner webhooks + run lifecycle are the only status
   sources
9. the agent does agent work: it fetches, posts, and writes its own prose — never
   render Jinja or thread metadata on its behalf; identity is the minimal key
10. one noun per concept: input = identity, state = learned facts; never compose
    bags
11. failures raise a typed error, never a sentinel return: no `value | error-string`
    union, no `X | None` as ok/fail, no `isinstance` on the error arm at the call
    site
12. wire boundary is minimal: single-field request body → `Body(..., embed=True)`,
    never a one-field `BaseModel`; a read-side response is a `from_attributes`
    projection that does no I/O — the route fetches and hands data in, a schema
    method never queries
13. positive conditions: `if value: do`, never `if not value:` bare-return followed
    by the happy path one positive branch could hold — flip it and let the miss
    fall through (negative guards stay for raises and real multi-exit chains)

Process: never pipe a test run that gates a commit — run tests as their own step.
After any scripted edit, grep that it landed. As the last step before commit,
re-read this list against every touched file. Make it mechanical — grep the touched
files for the tells:

- `from … import` inside a `def` (function-level import, no real cycle)
- `isinstance(` on data we produced
- `-> … | str` / `| None` used as ok/fail
- `class X(BaseModel)` with one field
- `def _helper` used once
- a query (`.get(` / `.all(` / `session`) inside a `schemas.py`/DTO method

## Craft gate

Write code that reads like prose. If a reader needs your explanation to follow it,
the code is wrong: fix the code, delete the explanation.

- Names carry the meaning. A function's name plus its signature should make its
  body predictable before you read it. Name things for what they ARE in the
  domain's words, never for their mechanism, their pattern, or their position in
  the pipeline. If you can't name it cleanly, you don't understand it yet — stop
  and re-derive the concept.
- No narration. No comment restating the code, no section banners, no "this
  handles X", no old-vs-new or transition notes. Code reads as the end state. What
  survives is a *why* that genuinely isn't visible in the code.
- One idea per function, at one altitude. Policy sitting next to plumbing is the
  tell. Behavior lives on the type that owns the data, not in a helper module.
- Shape it top-down: the happy path is the spine, exits are early, nesting is
  shallow, the ending is the interesting case.
- Prefer no new surface. A parameter or an inline beats a new function; a new
  function beats a new class; a new class beats a new package. Cheap to write is
  not a reason to exist.

The author-facing surface (extensions/SDK) is the product, not the plumbing.
Design it by writing the example first: the obvious call is the correct one, the
correct one is short, and a newcomer gets it right without reading the source or
the docs. No exposed internals, no required boilerplate, no ceremony, no knowledge
of framework mechanics leaking into app code. If the example needs a paragraph of
setup or a caveat, the surface is wrong — redesign it.

Before you report done: read every file you touched end to end, not the diff. If
any line makes you wince, that's the finding — fix it or say it out loud.
