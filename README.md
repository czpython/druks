<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/czpython/druks/main/docs/assets/logo/web/DruksLogo_White.svg" />
    <img src="https://raw.githubusercontent.com/czpython/druks/main/docs/assets/logo/web/DruksLogo_Black.svg" alt="Druks" width="140" />
  </picture>
</p>

# Druks

> [!WARNING]
> Druks is under active development. Expect breaking changes and rough edges
> before 1.0; `main` and `latest` are edge builds, not stable releases.

Druks is the self-hosted **home for durable agent apps**, running on the
Claude and Codex subscriptions you already pay for. Ship comes bundled:
autonomous software delivery from ticket to reviewed pull request.

An ordinary agent script loses its place when the process dies. A Druks
workflow records the result of each completed durable operation in Postgres.
After a restart or deploy, Druks replays the workflow and reuses those recorded
results instead of repeating completed work. If the process was interrupted
*inside* an operation, that operation may run again, so side effects still need
idempotency. [Durability and recovery](https://github.com/czpython/druks/blob/main/docs/concepts.md#durability-and-recovery)
explains the exact boundary.

## Install

The installer supports three deployment shapes backed by
[Drukbox](https://github.com/czpython/drukbox):

- `docker` (default): local sandbox containers on the host Docker daemon — the
  zero-config laptop shape
- `exe`: exe.dev sandbox VMs over a tailnet
- any other provider name: the generic remote shape

The default is the local shape, so the bare command boots a stack with no
authored values:

```bash
curl -fsSL https://druks.ai/install.sh | bash
```

That command follows the edge channel while Druks has no stable release. Once
versioned releases exist, install the script and image from the same tag as
described in [the release process](https://github.com/czpython/druks/blob/main/docs/releasing.md#install-an-immutable-version).
Re-running is also the upgrade path. Then follow
[full local setup](https://github.com/czpython/druks/blob/main/docs/full-local.md) to finish in
the dashboard: connect the agent harnesses and the GitHub App the bundled
`ship` app acts through; a standalone app may have different
integration requirements.

Or hand the install to a coding agent — paste this into Claude Code, Codex,
or any agent with shell access on the target machine:

> Install druks on this machine by following
> <https://raw.githubusercontent.com/czpython/druks/main/INSTALL.md>
> exactly: run every step's verification, and if one fails, stop and show me
> the failing step and its output instead of improvising.

For a remote install, name the provider — `exe` for exe.dev VMs, any other
Drukbox provider name for the generic remote shape:

```bash
curl -fsSL https://druks.ai/install.sh | DRUKS_PROVIDER=exe bash
```

The installer is non-interactive: the first run writes `~/druks/druks.toml`
with generated secrets, and when a remote shape still needs values only you
know (provider credentials, identity edge) it prints that checklist and exits;
set them in `druks.toml` and re-run the same command. See the
[deployment runbook](https://github.com/czpython/druks/blob/main/deploy/README.md)
for prerequisites, access control, verification, and rollback.

```text
trigger ──> app workflow ──> durable step ──> agent ──> sandbox
                 │                     │              │
                 │                     │              └─ Claude or Codex harness
                 │                     └─ result checkpointed in Postgres
                 ├─ event ──> feed / app reaction
                 └─ gate  ──> wait for human or external system ──> resume
```

**Platform and apps stay separate**

Druks owns the execution and operating substrate:

- DBOS workflows and queues backed by Postgres
- typed human gates, cancellation, schedules, and observable run state
- Claude and Codex harness dispatch through isolated Drukbox sandboxes
- append-only events, live feeds, webhooks, notifications, MCP servers, and skills
- validated operator settings, encrypted MCP/OAuth secrets, and the dashboard shell
- app discovery, API namespaces, and independent migration histories

An **app** owns its workflows, agents, domain models, routes, events, provider
reactions, and optional dashboard pages. It is a normal Python distribution
registered through the `druks.apps` entry-point group. Installing the
distribution registers it; Druks does not need an app-specific plugin list.

Scaffold one with the published CLI, no checkout required:

```bash
uvx --from druks druks create app night_watch
```

The generated project root carries an `AGENTS.md` with the contracts and a link
to the authoring guide.

The bundled `ship` app is a concrete example. It coordinates coding
agents through tickets and GitHub pull requests, but GitHub PR orchestration is
`ship` behavior—not the definition of Druks.

## Documentation

- **Evaluating Druks:** [Concepts and guarantees](https://github.com/czpython/druks/blob/main/docs/concepts.md)
- **Installing locally:** [Full local setup](https://github.com/czpython/druks/blob/main/docs/full-local.md)
- **Operating a remote stack:** [Deployment runbook](https://github.com/czpython/druks/blob/main/deploy/README.md)
- **Configuring integrations and secrets:** [Configuration](https://github.com/czpython/druks/blob/main/docs/configuration.md)
- **Building an app:** [Writing an app](https://github.com/czpython/druks/blob/main/docs/writing-an-app.md)
- **Diagnosing a run or service:** [Troubleshooting](https://github.com/czpython/druks/blob/main/docs/troubleshooting.md)
- **Contributing to Druks:** [Contribution guide](https://github.com/czpython/druks/blob/main/CONTRIBUTING.md)
- **Reporting a vulnerability:** [Security policy](https://github.com/czpython/druks/blob/main/SECURITY.md)
- **All documentation:** [Documentation index](https://github.com/czpython/druks/blob/main/docs/index.md)
