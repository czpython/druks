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

- `exe` (default): exe.dev sandbox VMs over a tailnet
- any provider name other than `exe` or `docker`: the generic remote shape
- `docker`: local sandbox containers, with Drukbox running on the host

For a remote install:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/czpython/druks/main/scripts/install.sh)
```

That command follows the edge channel while Druks has no stable release. Once
versioned releases exist, install the script and image from the same tag as
described in [the release process](https://github.com/czpython/druks/blob/main/docs/releasing.md#install-an-immutable-version).

The first run creates `~/druks/druks.toml`, generates secrets, renders `.env`,
and prints any values still required. Edit `druks.toml`, then re-run the same
command; it renders the complete `.env` artifact, pulls images, runs migrations,
and starts the stack. Re-running is also the upgrade path.
See the [deployment runbook](https://github.com/czpython/druks/blob/main/deploy/README.md) for prerequisites, access
control, verification, and rollback.

For a laptop-only stack:

```bash
DRUKS_PROVIDER=docker bash <(curl -fsSL https://raw.githubusercontent.com/czpython/druks/main/scripts/install.sh)
```

Then follow [full local setup](https://github.com/czpython/druks/blob/main/docs/full-local.md) to start Drukbox and connect
the agent harnesses. A complete installation needs GitHub Apps because the
bundled `ship` extension is installed; a standalone extension may have
different integration requirements.

```text
trigger ──> extension workflow ──> durable step ──> agent ──> sandbox
                 │                     │              │
                 │                     │              └─ Claude or Codex harness
                 │                     └─ result checkpointed in Postgres
                 ├─ event ──> feed / extension reaction
                 └─ gate  ──> wait for human or external system ──> resume
```

**Platform and applications stay separate**

Druks owns the execution and operating substrate:

- DBOS workflows and queues backed by Postgres
- typed human gates, cancellation, schedules, and observable run state
- Claude and Codex harness dispatch through isolated Drukbox sandboxes
- append-only events, live feeds, webhooks, notifications, MCP servers, and skills
- validated operator settings, encrypted MCP/OAuth secrets, and the dashboard shell
- extension discovery, API namespaces, and independent migration histories

An **extension** owns the application: its workflows, agents, domain models,
routes, events, provider reactions, and optional dashboard pages. It is a normal
Python distribution registered through the `druks.extensions` entry-point
group. Installing the distribution registers it; Druks does not need an
extension-specific plugin list.

Scaffold one with the published CLI, no checkout required:

```bash
uvx --from druks druks create extension night_watch
```

The generated project root carries an `AGENTS.md` with the contracts and a link
to the authoring guide.

The bundled `ship` extension is a concrete example. It coordinates coding
agents through tickets and GitHub pull requests, but GitHub PR orchestration is
`ship` behavior—not the definition of Druks.

## Documentation

- **Evaluating Druks:** [Concepts and guarantees](https://github.com/czpython/druks/blob/main/docs/concepts.md)
- **Installing locally:** [Full local setup](https://github.com/czpython/druks/blob/main/docs/full-local.md)
- **Operating a remote stack:** [Deployment runbook](https://github.com/czpython/druks/blob/main/deploy/README.md)
- **Configuring integrations and secrets:** [Configuration](https://github.com/czpython/druks/blob/main/docs/configuration.md)
- **Building an application:** [Writing an extension](https://github.com/czpython/druks/blob/main/docs/writing-an-extension.md)
- **Diagnosing a run or service:** [Troubleshooting](https://github.com/czpython/druks/blob/main/docs/troubleshooting.md)
- **Contributing to Druks:** [Contribution guide](https://github.com/czpython/druks/blob/main/CONTRIBUTING.md)
- **Reporting a vulnerability:** [Security policy](https://github.com/czpython/druks/blob/main/SECURITY.md)
- **All documentation:** [Documentation index](https://github.com/czpython/druks/blob/main/docs/index.md)
