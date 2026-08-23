# Druks documentation

Druks has three distinct audiences: operators run the platform, app
authors build apps on it, and contributors change Druks itself. Start
with the route that matches what you are doing.

## Understand the platform

- [Concepts and guarantees](concepts.md) — platform versus app ownership,
  durable execution, recovery, gates, events, harnesses, sandboxes, and the
  access boundary.
- [README](../README.md) — short project overview and installation entry point.

## Install and operate

- [Full local setup](full-local.md) — Druks and sandbox containers on one
  machine.
- [Deployment runbook](../deploy/README.md) — any Drukbox provider, install
  shapes, upgrades, rollback, logs, and public ingress.
- [Configuration](configuration.md) — `druks.toml`, process settings, dashboard
  settings, integrations, MCP, skills, and stored-secret handling.
- [Connect your agent](connect-your-agent.md) — the `/mcp` endpoint, personal
  access tokens, and client configuration.
- [Changelog](../CHANGELOG.md) — what each release added, changed, and removed.
- [Troubleshooting](troubleshooting.md) — symptom-driven diagnosis for boot,
  webhooks, harnesses, sandboxes, gates, and recovery.

## Build an app

- [Writing an app](writing-an-app.md) — scaffold a separately
  packaged app and use workflows, agents, gates, events, webhooks,
  settings, routes, and migrations.
- [Concepts and guarantees](concepts.md#the-app-boundary) — the ownership
  contract behind the author API.

## Contribute

- [Contributing](../CONTRIBUTING.md) — contribution process, change scope, and
  pull-request expectations.
- [Development](development.md) — local services, backend and frontend
  processes, architecture map, migrations, and verification.
- [Security policy](../SECURITY.md) — private vulnerability reporting.
- [Release process](releasing.md) — immutable image tags, release checks, and
  rollback inputs.
- [Open-source cut](open-source-cut.md) — one-time clean-history publication and
  public repository settings.
- [Frontend guide](../frontend/README.md) — dashboard shell, compile-time
  app UI registry, and frontend commands.

The repository intentionally uses plain Markdown rather than a documentation
framework. The pages above are the navigation; internal research and temporary
design notes do not belong in this index.
