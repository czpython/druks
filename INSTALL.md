# Install runbook

A coding agent can use this runbook to install the local shape on a target
machine. A human can also use it. [Full local setup](docs/full-local.md)
contains more explanation. The [deployment runbook](deploy/README.md) covers
remote shapes.

Do the steps in order. After each step, do its verification. If a verification
fails, stop. Report the failed step and its command output. Do not improvise a
fix. Do not continue the installation.

## 1. Preconditions

```bash
docker info --format '{{.ServerVersion}}'
docker compose version
```

Verification: both commands succeed. If a command fails, stop. Docker with the
Compose plugin is the only prerequisite. The operator must install it.

## 2. Install

```bash
curl -fsSL https://druks.ai/install.sh | bash
```

Verification: the command exits with status 0. Its final output includes
`docker compose up -d` and a message that the stack is up. `~/druks` contains
`druks.toml`, `.env`, and `compose.yaml`.

## 3. Services are up

```bash
cd ~/druks
docker compose ps
curl -fsS http://127.0.0.1:8001/health
```

Verification: `web`, `postgres`, `redis`, and `drukbox` operate without
restarts. The health endpoint returns `{"status":"ok"}`.

## 4. Preflight

```bash
cd ~/druks
docker compose exec web druks doctor
```

Verification: `druks doctor` exits with status 0. Each health check passes.
The Claude, Codex, and GitHub checks can show as pending (`○`). The operator
clears these checks in the browser.

A shell step cannot clear them, so they do
not fail the command. If the command exits with another status, stop. Report
the error.

## 5. Hand back to the operator

Report that the installation succeeded. Tell the operator to open
<http://127.0.0.1:8001>. Connect Claude and Codex under **Settings → Harnesses**.
Agent runs do not start with a disconnected harness. Then connect the GitHub
App that Druks uses. Run `docker compose exec web druks doctor --sandbox` to
prove the full sandbox path with a real container.

## Local customizations

Put host-local Docker Compose changes in `~/druks/compose.override.yaml`. Add
local services, service overrides, and named volumes to this file. `install.sh`
creates the file one time and does not change it. Your changes remain after an
install or upgrade.

`install.sh` refreshes the repository Compose files each time. Do not edit
`compose.yaml` or `compose.docker-sbx.yaml`. The next install overwrites
these files.

This example installs local apps in the web image:

```yaml
services:
  web:
    image: druks-with-apps
    build: ./appimage
```

Apply a change with `docker compose up -d` from `~/druks`.
