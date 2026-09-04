<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/czpython/druks/main/docs/assets/logo/web/DruksLogo_White.svg" />
    <img src="https://raw.githubusercontent.com/czpython/druks/main/docs/assets/logo/web/DruksLogo_Black.svg" alt="Druks" width="140" />
  </picture>
</p>

# Druks

> [!WARNING]
> Druks is under active development. Breaking changes and rough edges can occur
> before version 1.0. `main` and `latest` contain edge builds. They are not
> stable releases.

Druks is a self-hosted **home for durable agent apps**. It runs agents through
connected harnesses. The included Software Factory app automates software
delivery from a ticket to a reviewed pull request.

An ordinary agent script loses its place when the process dies. A Druks
workflow records the result of each completed durable operation in Postgres.
After a restart or deploy, Druks replays the workflow and reuses those recorded
results instead of repeating completed work. If the process stops *inside* an
operation, that operation can run again. Thus, side effects still require
idempotency. [Durability and recovery](https://docs.druks.ai/concepts#durability-and-recovery)
explains this boundary.

## Install

The installer supports three deployment shapes backed by
[Drukbox](https://github.com/czpython/drukbox):

- **Docker:** `docker` (default) starts local sandbox containers on the host Docker daemon.
- **Remote:** `exe` starts exe.dev sandbox VMs over a tailnet.
- Each other provider name selects the generic remote shape.

The default is the local shape, so the bare command boots a stack with no
authored values:

```bash
curl -fsSL https://druks.ai/install.sh | bash
```

That command follows the edge channel while Druks has no stable release.
Versioned releases use an installer script and an image from the same tag.
[The release process](https://docs.druks.ai/releasing#install-an-immutable-version)
explains this method. Run the installer again to upgrade Druks.

Then follow [full local setup](https://docs.druks.ai/full-local). Connect the
agent harnesses in the dashboard. Connect the GitHub App that
`software_factory` uses. A standalone app can require different integrations.

Or hand the install to a coding agent — paste this into Claude Code, Codex,
or any agent with shell access on the target machine:

> Install druks on this machine by following
> <https://raw.githubusercontent.com/czpython/druks/main/INSTALL.md>
> exactly. Run each verification step. If a verification fails, stop. Show me
> the failed step and its output. Do not improvise a fix.

For a remote install, name the provider — `exe` for exe.dev VMs, any other
Drukbox provider name for the generic remote shape:

```bash
curl -fsSL https://druks.ai/install.sh | DRUKS_PROVIDER=exe bash
```

The installer does not ask questions. The first run writes
`~/druks/druks.toml` with generated secrets. A remote shape can require values
that only you know. These values include provider credentials and identity-edge
details.

The installer prints this list and exits. Set the values in
`druks.toml`. Then run the same command again.

See the [deployment runbook](https://docs.druks.ai/deployment) for
prerequisites, access control, verification, and rollback.

```text
trigger ──> app workflow ──> durable step ──> agent ──> sandbox
                 │                     │              │
                 │                     │              └─ harness CLI
                 │                     └─ result checkpointed in Postgres
                 ├─ event ──> feed / app reaction
                 └─ gate  ──> wait for human or external system ──> resume
```

**Platform and apps stay separate**

Druks owns the execution and operating substrate:

- DBOS workflows and queues that use Postgres
- Typed human gates, cancellation, schedules, and observable run state
- Agent harness dispatch through isolated Drukbox sandboxes
- Append-only events, live feeds, webhooks, notifications, MCP servers, and skills
- Validated operator settings, encrypted MCP and OAuth secrets, and the dashboard shell
- App discovery, API namespaces, and independent migration histories.

An **app** owns its workflows, agents, domain models, routes, events, provider
reactions, and optional dashboard pages. It is a Python distribution that uses
the `druks.apps` entry-point group. Install the distribution to register it.
Druks does not require an app-specific plugin list.

Scaffold one with the published CLI, no checkout required:

```bash
uvx --from druks druks create app night_watch
```

The generated project root carries an `AGENTS.md` with the contracts and a link
to the authoring guide.

The bundled `software_factory` app is a concrete example. It coordinates coding
agents through tickets and GitHub pull requests. GitHub pull-request
orchestration belongs to `software_factory`, not to Druks.

## Documentation

- **Start here:** [Quickstart](https://docs.druks.ai/quickstart)
- **Understand recovery:** [Concepts and guarantees](https://docs.druks.ai/concepts)
- **Build an app:** [Author guide](https://docs.druks.ai/writing-an-app)
- **Operate Druks:** [Deployment](https://docs.druks.ai/deployment) and [configuration](https://docs.druks.ai/configuration)
- **Diagnose a failure:** [Troubleshooting](https://docs.druks.ai/troubleshooting)
- **Contributing to Druks:** [Contribution guide](https://github.com/czpython/druks/blob/main/CONTRIBUTING.md)
- **Reporting a vulnerability:** [Security policy](https://github.com/czpython/druks/blob/main/SECURITY.md)
- **All documentation:** [docs.druks.ai](https://docs.druks.ai).
