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
- Dashboard, Events, Usage, and Schedules
- Shared routing and fallback behavior.

Bundled app UI lives under `src/apps/<name>/`. Its module calls
`registerAppUI()` with routes and an optional home path. The backend app class
declares the subnav tabs. The roster supplies these tabs to the frontend.
Import the module one time from `src/apps/index.ts`. The shell finds the
registration and does not hardcode the app name.

The work sidebar keeps the same destinations across app pages. The Dashboard
opens at `/`. Events, Usage, and Schedules have shared routes. Schedules appears
directly below Usage. App-declared navigation appears below the page
header. Settings opens from the bottom of the sidebar. Below 650 px, a
navigation button opens a modal drawer. Escape closes the drawer and returns
focus to the button.

Settings use `/settings/<section>` routes. `/settings/personal` edits the current
account's preferences through `/api/settings/personal`. `/settings/agents` edits
shared execution defaults through `/api/settings`. The
preferences provider uses the personal endpoint for timestamp display. Preferences
contains only timezone and does not depend on execution catalogs. All accounts
use the shared execution defaults in Agents and the app agent overrides. Search
matches section names and app field labels. Preferences and Agents
retain separate drafts across settings pages. Save changes applies only the current page.
Leaving Settings offers Save, Discard, and Stay. Save applies each dirty page;
a failed request keeps the operator on that page with its draft. Resource
actions, such as connecting a provider or minting an API token, apply at once.
Back to Druks restores the previous work URL and keeps the work page mounted.

Connections groups Services, Accounts, Browser, and Revoked. Its `tab` query
parameter selects the active tab. The Browser profiles page manages saved browser state
and login windows at `/settings/connections?tab=browser`.

App settings use `/apps/<name>/settings` in the work context. A gear beside the
app name in the header opens this route. Settings search also links to app settings.
Options and Agents appear only when the app declares those controls. Both
sections share one app draft. Leaving the app form offers Save, Discard, and
Stay. An app without controls has no Settings destination. Backend app schemas
supply these forms without a frontend module. Schedule controls use the
existing workflow overrides.

Schedules at `/schedules` groups declared workflows by app. The `app` query
parameter filters the list. Operators change cadence and pause state here with
the same controls as app settings. Each schedule saves through
`PATCH /api/settings/apps`. Use defaults removes both overrides.

A failed save keeps the draft. Polling and focus refresh preserve unsaved edits.
If you leave the page with unsaved edits, the shell asks first, as Settings
does. The page shows the installation timezone beside the saved cadence.

Normal interface text uses IBM Plex Sans at 15 px. Technical values use
IBM Plex Mono. Phone inputs use at least 16 px.

Backend and frontend app discovery are intentionally separate:

- Python entry points load an installed backend app at runtime.
- Vite compiles React app modules into the SPA at build time.

An installed Python distribution cannot put JavaScript into an existing
dashboard build. A backend-only app can still use the platform API, settings,
events, generic subject read-side, and declared Python pages. Custom React
pages require a dashboard build that contains the UI module.

A separate app package can ship a built ES module in `<package>/dist/`. This
module exposes `mount(el, ctx)`. Druks serves the module under `/app/<name>`.
The shell imports and mounts it below the chrome. An import map (`src/runtime/`)
supplies one shared React instance. See the app-author guide.

## Dashboard and owner links

The Dashboard reads `/api/dashboard/overview` every 30 seconds and on window
focus. The `app` query parameter filters exact totals, previews, and recorded
timestamps on the server.

Requests get the main space. If there are no requests, failures get it. If there
are no failures, running work gets it. The main section shows at most four
cards. The compact status panel shows the other totals and at most two names
per state. Overflow is plain text, and the Dashboard has no full-list
destination.

Initial loading and read failure do not show an empty result. A failed refresh
keeps the last successful read visible, marks it stale, and offers Retry.
Recovery clears the stale message. The account timezone controls the greeting
and the time labels. If the API supplies a recorded timestamp, the Dashboard
shows its age. See [the current-work contract](../docs/concepts.md#current-work-on-the-dashboard)
for selection, authorization, and limits.

An app's `subjectPath(subject, target?)` returns its own destination. For
the Dashboard, `target` carries `run` and, for a decision, `parkedAt`. Build the
query with `targetQuery` from the registry. The owner selects that run and
passes `parkedAt` to `GateControls`. If the current round is different,
`GateControls` shows a stale-link message.

If the app has no destination, return `undefined`. The Dashboard then keeps the
context of the card without an action. External requests open only their
supplied HTTP(S) URL. The home page has no decision controls.

Python apps can select a decision page with
[`@ui.page(..., subject=...)`](../docs/druks-ui.md#declare-pages).
The roster exposes this declaration as `PageEntry.subjectType`. The shell fills
the route parameter and keeps the exact target. Without this declaration, the
generic subject page opens. An app's standalone JavaScript frontend supplies
its own navigation to a specific run.

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
