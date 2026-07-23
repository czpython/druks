# Druks documentation

Druks has three distinct audiences: operators run the platform, extension
authors build applications on it, and contributors change Druks itself. Start
with the route that matches what you are doing.

## Understand the platform

- [Concepts and guarantees](concepts.md) — platform versus extension ownership,
  durable execution, recovery, gates, events, harnesses, sandboxes, and the
  access boundary.
- [README](../README.md) — short project overview and installation entry point.

## Install and operate

- [Full local setup](full-local.md) — Druks and sandbox containers on one
  machine.
- [Deployment runbook](../deploy/README.md) — remote `exe` or `aws` install,
  upgrades, rollback, logs, and public ingress.
- [Configuration](configuration.md) — environment settings, dashboard settings,
  integrations, MCP, skills, and stored-secret handling.
- [Connect your agent](connect-your-agent.md) — the `/mcp` endpoint, personal
  access tokens, and client configuration.
- [Troubleshooting](troubleshooting.md) — symptom-driven diagnosis for boot,
  webhooks, harnesses, sandboxes, gates, and recovery.

## Build an extension

- [Writing an extension](writing-an-extension.md) — scaffold a separately
  packaged application and use workflows, agents, gates, events, webhooks,
  settings, routes, and migrations.
- [Concepts and guarantees](concepts.md#the-extension-boundary) — the ownership
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
  extension UI registry, and frontend commands.

The repository intentionally uses plain Markdown rather than a documentation
framework. The pages above are the navigation; internal research and temporary
design notes do not belong in this index.
