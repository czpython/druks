# Druks Deployment

The whole stack runs in Docker Compose: Druks (`web`, which embeds the DBOS
durable engine and serves the dashboard SPA), Postgres, and Redis. A
**remote** install (`DRUKS_PROVIDER=exe`/`aws`) also brings up stock Caddy
(identity edge + proxy, Caddyfile bind-mounted) and the Drukbox sandbox control
plane (`sandbox-service`, `sandbox-janitor`). Those three live in the compose
`remote` profile, which `install.sh` selects by writing `COMPOSE_PROFILES` to
`.env`, so plain `docker compose` commands in the install dir pick it up.

A **local** install (`DRUKS_PROVIDER=docker`) runs neither: the dashboard is
reached directly on `127.0.0.1:8001` and sandboxes are local Docker
containers with drukbox on the host — see
[Full local](../docs/full-local.md).

The Druks services use the **host network** so they can reach Postgres, Redis,
Drukbox, and provider-specific sandbox addresses from the host network
namespace. The default exe.dev profile reaches VMs over the host's tailnet;
other providers may return directly reachable SSH addresses.

## First-time setup on a fresh box

Prerequisites: Docker with the Compose plugin. The default exe.dev profile also
needs `tailscaled` joined to the intended tailnet (`tailscale status` shows
peers). AWS uses its own network and credential block; the local Docker profile
is covered in [Full local](../docs/full-local.md).

The Druks application and sandbox images are published for both `linux/amd64`
and `linux/arm64`.

Everything else — `compose.yaml`, the Caddyfile, `.env`, image pulls,
DB init — is handled by `install.sh`. (While the ghcr.io images
require auth, export `GHCR_TOKEN` — a PAT with `read:packages` — and
the installer logs in with it.)

### 1. Run the installer

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/czpython/druks/main/scripts/install.sh)
```

First pass writes `~/druks/.env` with random secrets pre-filled,
creates `~/druks/secrets/`, and exits. It tells you exactly what to do
next:

- Fill in the blanks at the top of `.env` (the sandbox provider block).
- Provision the two GitHub Apps: re-run the installer with `--apps`.
  It registers each app via GitHub's manifest flow — it prints a link
  per app, you open it, click Create, then paste the `?code=...` from
  the redirect back into the SSH session. App ids, PEMs, and the
  webhook secret are written in place. (Manual fallback: create the
  apps by hand per the permission tables in
  [`docs/configuration.md`](../docs/configuration.md) and upload the
  PEMs to `~/druks/secrets/{operator,reviewer}.pem`.)
The sandbox backend defaults to exe.dev + Tailscale. Pass `DRUKS_PROVIDER`
on the first run to choose another: `aws` (EC2 — the `.env` carries the
`AWS_*` block of region, AMI, and credentials instead) or `docker` (local
sandboxes, drukbox on the host; see [Full local](../docs/full-local.md)).
Re-runs read the choice back from `.env`, so you only set it once.

Override the install dir with `DRUKS_INSTALL_DIR=/srv/druks` if you
want it elsewhere.

### 2. Re-run the installer

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/czpython/druks/main/scripts/install.sh)
```

Second pass validates that the required `.env` keys and PEMs are present,
then: `docker compose pull` → migrate the databases out of band
(`docker compose run --rm web druks init-db`, plus drukbox's schema on a
remote install) → `docker compose up -d`. Nothing migrates on boot.

### 3. Verify

```bash
cd ~/druks
docker compose ps
curl -fsS http://127.0.0.1:8001/health
```

Then connect Claude and Codex from the dashboard (Settings →
Harnesses → Connect): each card opens the provider's authorize page
and takes the pasted code back. Subscription tokens live in the
database — no host CLI login — and agent runs refuse to start on a
harness that isn't connected.

After both connections:

```bash
docker compose exec web druks doctor
```

Every configured check should pass. Before the connections, the same command
is still useful for infrastructure but correctly reports both harness
credential checks as failures.

### 4. Expose the public surfaces

**On exe.dev**, one port-share carries both dashboard and webhooks:

```bash
ssh exe.dev share port druks 8000
ssh exe.dev share set-public druks
```

Public URLs: `https://<host>/_external/{github,linear,jira}/events/`
(HMAC-gated webhooks), `https://<host>/mcp` (PAT-authenticated MCP endpoint —
[Connect your agent](../docs/connect-your-agent.md)), and `https://<host>/`
(exe.dev authenticates at the edge; druks maps its asserted email to your
account).

**Elsewhere (e.g. AWS + Teleport)**, the dashboard goes through your
identity proxy (set `DRUKS_AUTH_HEADER` to the header it injects), but
webhook senders can't authenticate through SSO — they need their own
public HTTPS path. The stack's Caddy provides it:

The proxy must strip any client-supplied copy of that identity header before
inserting its authenticated value. It must also terminate TLS and set HSTS;
Druks' shipped Caddy dashboard listener is loopback HTTP behind that edge.

1. Point an A-record (e.g. `druks.example.com`) at the box and open
   inbound 80 + 443.
2. Set `DRUKS_WEBHOOK_HOST=druks.example.com` in `.env` and
   `docker compose up -d caddy`.

Caddy auto-provisions Let's Encrypt for that hostname and serves **only**
`POST /_external/*` and the PAT-authenticated `/mcp` endpoint on it — no
dashboard routes, no identity header, so the SSO gate can't be forged from
the public side. Webhook URLs become
`https://druks.example.com/_external/<provider>/events/`; agents connect at
`https://druks.example.com/mcp`
([Connect your agent](../docs/connect-your-agent.md)). Leave
`DRUKS_WEBHOOK_HOST` unset to bring your own ingress instead.

## Update / redeploy

Re-run the installer from the same version you intend to deploy. It always
refreshes `compose.yaml` and the Caddyfile
from the repo, applies any new migrations out of band
(`docker compose run --rm web druks init-db`, plus drukbox's on a remote
install), then runs `docker compose pull && up -d`, so it is the upgrade
path. Compose recreates only changed services. To migrate by hand without the
installer, run that same `docker compose run --rm web druks init-db`.

Recreating `web` interrupts in-flight execution. DBOS recovers compatible
workflows from completed checkpoints when the process returns. Treat changes to
workflow structure, step order or names, and serialized input as deployment
compatibility changes: drain affected runs or keep an executor with compatible
code until they finish. Recovery does not preserve a live agent process inside
a sandbox; it follows the operation boundary described in
[Concepts](../docs/concepts.md#durability-and-recovery).

### One-time: upgrading a box that ran the backend as root

The backend containers now run as the deploy user (`DRUKS_UID`/`DRUKS_GID`),
not root. A box deployed before this change has root-owned files the non-root
containers must write — the `logs/` + `prompt-cache/` dirs under the data dir.
`install.sh` sets `DRUKS_UID`/`DRUKS_GID` and chowns the sandbox-keys volume on
re-run, but it runs unprivileged, so take over the root-owned host files once
first (adjust the path if you customized `DRUKS_DATA_HOST_DIR`):

```bash
docker run --rm -v "$HOME/druks-data:/d" alpine chown -R "$(id -u):$(id -g)" /d
```

A root container chowns the bind-mounted host paths, so no host `sudo`. Then
run the normal upgrade (re-run `install.sh`) — it writes `DRUKS_UID`/`DRUKS_GID`,
chowns the sandbox-keys volume, and recreates the stack as the deploy user. New
installs need none of this: `install.sh` and the deploy user own everything
from the start.

`main` and `latest` are the edge channel. For a tagged install, fetch the
installer from that tag and set the same `DRUKS_REF`; it automatically selects
the matching image tag. See [Releasing Druks](../docs/releasing.md) for the
immutable install shape.

## Rollback

The Druks image is tagged `:sha-<full-git-sha>` per commit and carries both
the API and the SPA build — one artifact, nothing to keep in lockstep.
Pin a specific build by setting `DRUKS_TAG` in `.env`:

```bash
DRUKS_TAG=sha-0123456789abcdef0123456789abcdef01234567 docker compose up -d
```

This rolls back the image, not the database schema. `druks init-db` only
upgrades; it does not downgrade migrations. Before pinning an older image,
confirm that its code can read the current schema and that in-flight workflows
are compatible with that code.

## Logs / stop

```bash
docker compose logs -f web
docker compose logs -f sandbox-service sandbox-janitor   # remote install only
docker compose down
```

## How the proxy routes

exe.dev exposes one port; Caddy (stock image, host network, `:8000`,
Caddyfile fetched by the installer) enforces path-level access:

- `POST /_external/*` — public, authenticated by the matching webhook class in
  Druks. Per-provider paths land under
  `/_external/<provider>/<category>/`; extension role-module discovery
  registers them at import time.
- `/mcp` — public, authenticated per request by personal access token inside
  the app; proxied unbuffered so its SSE frames stream.
- Everything else — a nonempty trusted identity header (exe.dev login
  provides one) required, then proxied to `web` (`127.0.0.1:8001`), which
  serves the API, the SPA, and extension frontends alike; the app maps that
  asserted email to your account per request
  ([access control](../docs/configuration.md#public-urls-and-access-control)).
