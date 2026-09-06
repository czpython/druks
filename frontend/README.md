# Druks frontend

The frontend is the React 19 dashboard in the Druks backend image. FastAPI
serves the built SPA in production. Vite operates separately during development
and proxies API calls to the backend.

## Commands

From the repository root:

```bash
npm --prefix frontend ci
npm --prefix frontend run dev
npm --prefix frontend run lint
npm --prefix frontend test
npm --prefix frontend run build
```

`build` runs TypeScript project compilation before Vite. CI uses Node 22 and
runs lint, tests, and build for PRs into `main` and `codex/` stack branches.

## Ownership

`src/App.tsx` is the platform shell. It owns:

- The work sidebar and searchable installed app roster
- Settings
- Dashboard, Events, and Usage
- Shared routing and fallback behavior.

Bundled app UI lives under `src/apps/<name>/`. Its module calls
`registerAppUI()` with routes and an optional home path. The backend app class
declares the subnav tabs. The roster supplies these tabs to the frontend.
Import the module one time from `src/apps/index.ts`. The shell finds the
registration and does not hardcode the app name.

The work sidebar keeps the same destinations across app pages. The Dashboard
opens at `/`. Events and Usage have shared routes. App-declared navigation appears below the page
header. Settings opens from the bottom of the sidebar. Below 650 px, a
navigation button opens a modal drawer. Escape closes the drawer and returns
focus to the button.

Shared settings use `/settings/<section>` routes. Search matches section names
and app field labels. General and Agent defaults retain separate
drafts across settings pages. Save changes applies only the current page.
Leaving Settings offers Save, Discard, and Stay. Save applies each dirty page;
a failed request keeps the operator on that page with its draft. Resource
actions, such as connecting a provider or minting an API token, apply at once.
Back to Druks restores the previous work URL and keeps the work page mounted.

App settings use `/apps/<name>/settings` in the work context. The app's page
navigation and the central App settings index link to this same route.
Options and Agents appear only when the app declares those controls. Both
sections share one app draft. Leaving the app form offers Save, Discard, and
Stay. An app without controls has no Settings destination. Backend app schemas
supply these forms without a frontend module. Schedule controls use the
existing workflow overrides.

Normal interface text uses IBM Plex Sans at 15 px. Technical values use
IBM Plex Mono. Phone inputs use at least 16 px.

Backend and frontend app discovery are intentionally separate:

- Python entry points load an installed backend app at runtime.
- Vite compiles React app modules into the SPA at build time.

An installed Python distribution cannot put JavaScript into an existing
dashboard build. A backend-only app can still use the platform API, settings,
events, and generic subject read-side. Custom pages require a dashboard build
that contains the UI module.

A separate app package can ship a built ES module in `<package>/dist/`. This
module exposes `mount(el, ctx)`. Druks serves the module under `/app/<name>`.
The shell imports and mounts it below the chrome. An import map (`src/runtime/`)
supplies one shared React instance. See the app-author guide.

## Dashboard and owner links

The Dashboard makes one current-work read and one schedule read, refreshed every
30 seconds and on window focus, and sorts the rows into sections in the
browser. A failed refresh keeps the last read visible and offers Retry. See
[the current-work contract](../docs/concepts.md#current-work-on-the-dashboard) for
selection, authorization, and limits.

An app's `subjectPath(subject, target?)` returns its own destination. For
the Dashboard, `target` carries `run` and, for a decision, `parkedAt`. Build the
query with `targetQuery` from the registry. The owner selects that run and
passes `parkedAt` to `GateControls`, which shows a stale-link message when the
current round differs. Return `undefined` when the app has no destination.

The generic Python-app subject page supports this target. An app's standalone
JavaScript frontend must supply its own navigation before the Dashboard can link to
a specific run.

Keep raw paths and queries in the retained work context. Wouter's public
`useLocation` and `useSearch` decode URI escapes. Subject pages read the raw
router hooks, decode each subject component once, and let `subjectApi` encode
the HTTP path. Canonical slug replacement preserves the raw query and hash
and only runs while the owner page is visible.

## API and live data

Shared requests use `src/api/client.ts`. The event feed and transcript
components consume server-sent events. Standard HTTP queries supply the initial
state. Keep API field names aligned with the camelCase `Schema` output from the
backend.

If a backend response contract changes, update the TypeScript type, consumer,
and focused frontend test in the same change. The `types:openapi` script is
experimental and requires an active server. The repository does not contain
generated OpenAPI types.

## Development topology

Use [the development guide](../docs/development.md) to start Postgres, Redis,
the backend, and Vite. To examine production-like static assets, run
`npm --prefix frontend run build`. Then start the backend. The server serves the
repository-root `dist/` directory when it exists.
