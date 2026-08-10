# Changelog

All notable changes to Druks. Versions follow [semantic versioning](https://semver.org);
while Druks is pre-1.0, a minor bump may break compatibility.

## [0.2.0] — 2026-08-10

### Added

- **One-command install.** `curl -fsSL https://druks.ai/install.sh | bash` boots
  the local shape: Docker sandboxes by default, no prompts, no TTY. The local
  drukbox runs in compose on the same Postgres, so there is no second datastore.
- **Agent-runbook install.** `INSTALL.md` walks the install as a deterministic
  sequence a coding agent can follow; the README carries the prompt to paste in.
- **GitHub App creation from the dashboard.** The connect card creates the App
  through GitHub's manifest flow and stores its credentials. Installing the App
  is the only step left on GitHub.
- **Service identities.** Connections to outside services live in encrypted
  rows an extension can declare. GitHub and the trackers ride them, and a
  Services settings pane shows each connection as a card.
- **Agents find and start work.** `list_open_subjects` shows an agent what is
  in flight, `ship_start` triggers a ticket, and the surface teaches its own
  contract.
- **Adaptive plan gate.** The planner reports its confidence. A confident plan
  the critic approved implements without parking; anything less waits for the
  operator.
- **Reviews without a review identity.** When no review account is connected,
  reviews post as operator comments instead of failing the run.
- **Sandbox commits carry identity.** Commits are authored by the connected
  App's bot, with the dispatching account as co-author.

### Changed

- **Plans are briefings.** One critic pass, one redraft, outcome-level
  acceptance criteria, one screenful. A parked critique survives an operator
  redraft.
- **One review per revision.** The code-review pass folds into the
  implementation review as a second lens, and one GitHub review carries the
  verdict and the notes.
- **The PR body is written once** and republished on every revision, and it
  states the acceptance criteria.
- **`ship_start` stamps the tracker.** It moves the ticket to the trigger
  status and lets webhook intake open the build. A ticket Druks has never seen
  is a typed 404.
- **Settings panes share one design.** Services, Harnesses, and Skills use one
  type scale, one card surface, one status indicator, one chevron.
- **The agent call page shows the call's artifact.** A plan renders on the call
  page, not only while its run sits parked.
- **Project delete confirms, then cascades.** Deleting a project removes its
  work items after a confirmation that names the scope, instead of a silent 409.
- **`druks doctor` exits non-zero only on a genuine fault.** Pending operator
  setup — connect GitHub, connect a harness — reports as pending and exits 0.

### Fixed

- **Live transcripts parse incrementally** instead of re-parsing the whole
  buffer twice a second.
- **Ticket intake is idempotent.** A re-delivered "Ready for Agent" webhook no
  longer re-builds a merged item.
- **Cancelled builds settle their work items.**
- **Pinned verification commands keep the CI check that proves them.**

## [0.1.0] — 2026-08-02

### Added

- **Multi-tenancy.** Each person signs in through the edge, connects their own
  Claude and Codex subscriptions, and sees their own quota and spend.
- **MCP endpoint.** Druks serves `/mcp`. Point an agent at it with a personal
  access token to answer gates, retry runs, and read usage.
- **MCP server installs.** Search the official registry from Settings and install
  a server by name, connected once for everyone or separately per account.
- **`druks.toml`.** One authored config file; the installer renders `.env` from it.
- **Review extension.** A second bundled app. It reviews a pull request against a
  checkout of the repo and posts one GitHub review.
- **Plan gate.** Choose who approves a plan: a human, the machine, or the machine
  and then a human.
- **Failure recovery.** Agent calls retry transient harness failures and wait out
  quota windows. A run that died can be retried from the step that killed it.
- **Encryption at rest** for harness credentials, MCP headers, and secret settings.
- **Extension testing.** Installing Druks registers pytest fixtures, so an
  extension tests itself without a Druks checkout.

### Changed

- **`build` is now `ship`**, packaged under `druks.contrib.ship` and served at
  `/api/ship`.
- **Trackers belong to ship.** An installation names one tracker and holds its
  credentials in the dashboard.
- **Subjects.** A workflow declares what its runs are about, and the subject
  answers for its own status, timeline, and board.
- **The journal replaces workflow state.** A run records typed facts and
  announces them.
- **Models come from the provider**, not a list shipped in the code.
- **The events feed is one global timeline.**

### Removed

- **The scoper** — no brief agent, no refinement statuses.
- **Environment-variable configuration.** Secrets, endpoints, GitHub App ids, and
  tracker credentials moved to `druks.toml` and dashboard settings.
- **`druks.ticketing`** from the platform. A tracker now only pushes status.
- **`Workflow.set_state()`, `Extension.format_event()`, and
  `Extension.subject_type`**, replaced by the journal, feed facts, and the
  subject class.
- **Jira remote links and sub-task creation.**

## [0.0.1] — 2026-07-12

Initial public release.
