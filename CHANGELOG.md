# Changelog

All notable changes to Druks. Versions follow [semantic versioning](https://semver.org);
while Druks is pre-1.0, a minor bump may break compatibility.

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
