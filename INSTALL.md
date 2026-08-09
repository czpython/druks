# Install runbook

A deterministic install of the local (laptop) shape, written for a coding
agent operating a shell on the target machine. Humans are welcome too — the
narrative version is [docs/full-local.md](docs/full-local.md), and remote
shapes are covered by [deploy/README.md](deploy/README.md).

Rules: run the steps in order; after each step run its verification exactly;
if a verification fails, stop and report the failing step with its command
output — do not improvise a fix or continue.

## 1. Preconditions

```bash
docker info --format '{{.ServerVersion}}'
docker compose version
```

Verification: both commands succeed. If either fails, stop — Docker with the
Compose plugin is the one prerequisite, and installing it is the operator's
call, not yours.

## 2. Install

```bash
curl -fsSL https://druks.ai/install.sh | DRUKS_PROVIDER=docker bash
```

Verification: the command exits 0 and ends with `docker compose up -d`
output followed by a message that the stack is up. `~/druks` now contains
`druks.toml`, `.env`, and `compose.yaml`.

## 3. Services are up

```bash
cd ~/druks
docker compose ps
curl -fsS http://127.0.0.1:8001/health
```

Verification: `web`, `postgres`, `redis`, and `drukbox` are running
(none restarting), and the health endpoint returns `{"status":"ok"}`.

## 4. Preflight

```bash
cd ~/druks
docker compose exec web druks doctor
```

Verification: every check passes except the Claude, Codex, and GitHub
connection checks — those correctly fail until the operator connects them in
the browser, which no shell step can do.

## 5. Hand back to the operator

Report that druks is installed and tell the operator to finish in the
dashboard at <http://127.0.0.1:8001>: **Settings → Harnesses** connects
Claude and Codex (agent runs refuse to start on an unconnected harness) and
the GitHub App druks acts as. Then `docker compose exec web druks doctor
--sandbox` proves the full sandbox path with a real container.
