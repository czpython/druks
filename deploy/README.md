# Druks Deployment

`compose.yaml` holds the full stack: Druks (`web`, which embeds the DBOS
durable engine and serves the dashboard SPA), Postgres, Redis, the Drukbox
sandbox control plane (`drukbox`), the janitor, the SSH gateway, and the Caddy
edge. `install.sh` writes `COMPOSE_PROFILES` to `.env`. Then plain
`docker compose` commands in the install directory do the correct thing.

A **local** install (`DRUKS_PROVIDER=docker`) runs bare, with no profiles.
`drukbox` mounts the Docker socket of the host. Sandboxes are sibling
containers on the host daemon. The dashboard is on `127.0.0.1:8001`, with no
Caddy. See [Full local](../docs/full-local.md).

A **remote** install (each other `DRUKS_PROVIDER`) enables
`COMPOSE_PROFILES=hosted`: the Drukbox control plane against a cloud provider,
the periodic janitor, and stock Caddy (identity edge and proxy, with the
Caddyfile bind-mounted).

The **docker-sbx** provider also layers `compose.docker-sbx.yaml` and enables
the `gateway` profile. The overlay connects the Drukbox services to the
[Docker Sandboxes](https://docs.docker.com/ai/sandboxes/) daemon of the host
(microVM sandboxes). The gateway is the SSH path into them. Prepare the host
first: install `docker-sbx`, put the service user in the `kvm` group, then run
`sbx login` and `sbx daemon start -d --policy balanced`. The installer stops
with a clear message when the daemon socket is missing.

Before this layout, each shape had its own overlay file. Those files are
retired: `compose.local.yaml` is now the base with no profiles, and
`compose.remote.yaml` is the base with `COMPOSE_PROFILES=hosted`. Deployments
that fetch compose files by path must update to `compose.yaml` (and
`compose.docker-sbx.yaml` for that provider).

Drukbox keeps its own schema in a `drukbox` database in the same Postgres, so
there is no second datastore to run or back up separately.

The Druks services use the **host network** so they can reach Postgres, Redis,
Drukbox, and provider-specific sandbox addresses from the host network
namespace. The exe.dev shape reaches VMs over the host's tailnet;
other providers may return directly reachable SSH addresses.

## First-time setup on a fresh box

Prerequisites: Docker with the Compose plugin. The exe.dev shape also
needs `tailscaled` joined to the intended tailnet (`tailscale status` shows
peers). Other remote providers have their own network and credential
requirements; the local Docker shape is covered in
[Full local](../docs/full-local.md).

The Druks service and sandbox images are published for both `linux/amd64`
and `linux/arm64`.

Everything else — `compose.yaml`, the Caddyfile, `druks.toml`, the rendered
`.env`, image pulls, and DB init — is handled by `install.sh`.

### 1. Run the installer

```bash
curl -fsSL https://druks.ai/install.sh | DRUKS_PROVIDER=exe bash
```

`docker` is the default provider (the local shape); a remote deploy names its
provider — `exe` for exe.dev, any other Drukbox provider name for the generic
remote shape. First pass writes `~/druks/druks.toml` with random secrets
pre-filled, renders `~/druks/.env`, and exits when required values are missing.
It tells you exactly what to do next: edit `druks.toml` — for a generic remote
shape, fill `[sandbox.<provider>]` from Drukbox's
[configuration reference](https://github.com/czpython/drukbox).

The GitHub App druks acts as is connected from the dashboard after boot
(**Settings → Harnesses → Connect GitHub**), per the permission table in
[`docs/configuration.md`](../docs/configuration.md#github).

The sandbox backend defaults to the local `docker` shape. Pass `DRUKS_PROVIDER`
on the first run to choose another: `exe` for exe.dev + Tailscale, any other
Drukbox provider name for the generic remote shape (passed through without a
Druks provider registry). Re-runs read `[sandbox].provider` from `druks.toml`,
so the environment flag is only a fresh-install seed.

Override the install dir with `DRUKS_INSTALL_DIR=/srv/druks` if you
want it elsewhere.

### 2. Re-run the installer

```bash
curl -fsSL https://druks.ai/install.sh | bash
```

Second pass renders `.env` from `druks.toml`, validates the required values,
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
(authenticated provider webhooks), `https://<host>/mcp` (PAT-authenticated MCP endpoint —
[Connect your agent](../docs/connect-your-agent.md)), and `https://<host>/`
(exe.dev authenticates at the edge; druks maps its asserted email to your
account).

**On another remote provider**, the dashboard goes through your identity proxy
(set `[identity].header` in `druks.toml` to the header it injects), but
webhook senders can't authenticate through SSO — they need their own
public HTTPS path. The stack's Caddy provides it:

The proxy must strip any client-supplied copy of that identity header before
inserting its authenticated value. It must also terminate TLS and set HSTS;
Druks' shipped Caddy dashboard listener is loopback HTTP behind that edge.

1. Point an A-record (e.g. `druks.example.com`) at the box and open
   inbound 80 + 443.
2. Set `[urls].webhook_host = "druks.example.com"` in `druks.toml` and re-run
   the installer.

Caddy auto-provisions Let's Encrypt for that hostname and serves **only**
`POST /_external/*` and the PAT-authenticated `/mcp` endpoint on it — no
dashboard routes, no identity header, so the SSO gate can't be forged from
the public side. Webhook URLs become
`https://druks.example.com/_external/<provider>/events/`; agents connect at
`https://druks.example.com/mcp`
([Connect your agent](../docs/connect-your-agent.md)). Leave
`[urls].webhook_host` blank to bring your own ingress instead.

## Update / redeploy

Edit `druks.toml`, then re-run the installer from the same version you intend
to deploy. It renders `.env`, refreshes `compose.yaml` and the Caddyfile
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
confirm that its code can read the current schema, the current `druks.toml`
and rendered `.env`, and that in-flight workflows are compatible with that
code.

## Logs / stop

```bash
docker compose logs -f web
docker compose logs -f drukbox               # the sandbox control plane
docker compose down
```

## How the proxy routes

exe.dev exposes one port; Caddy (stock image, host network, `:8000`,
Caddyfile fetched by the installer) enforces path-level access:

- `POST /_external/*` — public, authenticated by the matching webhook class in
  Druks. Per-provider paths land under
  `/_external/<provider>/<category>/`; app role-module discovery
  registers them at import time.
- `/mcp` — public, authenticated per request by personal access token inside
  Druks; proxied unbuffered so its SSE frames stream.
- Everything else — a nonempty trusted identity header (exe.dev login
  provides one) required, then proxied to `web` (`127.0.0.1:8001`), which
  serves the API, the SPA, and app frontends alike; Druks maps that
  asserted email to your account per request
  ([access control](../docs/configuration.md#public-urls-and-access-control)).
