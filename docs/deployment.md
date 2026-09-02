---
title: "Deploy Druks"
description: "Install, expose, upgrade, and roll back a production Druks stack."
sidebarTitle: "Deployment"
icon: "server"
---

`compose.yaml` holds the full stack. It includes Druks, Postgres, Redis,
Drukbox, the janitor, the SSH gateway, and the Caddy edge. The `web` service
contains the DBOS durable engine and serves the dashboard SPA. The `drukbox`
service is the sandbox control plane. `install.sh` writes `COMPOSE_PROFILES` to
`.env`. Then plain
`docker compose` commands in the install directory do the correct thing.

A **local** install (`DRUKS_PROVIDER=docker`) runs bare, with no profiles.
`drukbox` mounts the Docker socket of the host. Sandboxes are sibling
containers on the host daemon. The dashboard is on `127.0.0.1:8001`, with no
Caddy. See [Full local](full-local.md).

A **remote** installation uses each other `DRUKS_PROVIDER` value. It enables
`COMPOSE_PROFILES=hosted`. This profile includes a remote Drukbox control plane,
the periodic janitor, and stock Caddy. Caddy supplies the identity edge and
proxy with a bind-mounted Caddyfile.

The **docker-sbx** provider also layers `compose.docker-sbx.yaml` and enables
the `gateway` profile. The overlay connects the Drukbox services to the
[Docker Sandboxes](https://docs.docker.com/ai/sandboxes/) daemon of the host
(microVM sandboxes). The gateway is the SSH path into them.

Prepare the host first. Install `docker-sbx`. Put the service user in the `kvm` group. Run
`sbx login`. Then run `sbx daemon start -d --policy balanced`. The installer stops
with a clear message when the daemon socket is missing.

Before this layout, each shape had its own overlay file. Those files are
retired. `compose.local.yaml` is the base with no profiles.
`compose.remote.yaml` is the base with `COMPOSE_PROFILES=hosted`. Deployments
that fetch compose files by path must update to `compose.yaml` (and
`compose.docker-sbx.yaml` for that provider).

Drukbox keeps its own schema in a `drukbox` database in the same Postgres, so
there is no second datastore to run or back up separately.

The Druks services use the **host network** so they can connect to Postgres, Redis,
Drukbox, and provider-specific sandbox addresses from the host network
namespace. The exe.dev shape reaches VMs over the host tailnet. Other providers
can return SSH addresses that are directly reachable.

## First-time setup on a fresh box

Prerequisites: Docker with the Compose plugin. The exe.dev shape also
needs `tailscaled` joined to the intended tailnet (`tailscale status` shows
peers). Other remote providers have their own network and credential
requirements.

For the local Docker shape, see [Full local](full-local.md).

The image registry provides Druks service and sandbox images for both
`linux/amd64` and `linux/arm64`.

`install.sh` handles everything else. This work includes `compose.yaml`, the
Caddyfile, `druks.toml`, the rendered `.env`, image pulls, and DB initialization.

### 1. Run the installer

```bash
curl -fsSL https://druks.ai/install.sh | DRUKS_PROVIDER=exe bash
```

`docker` is the default provider for the local shape. A remote deployment names
its provider. Use `exe` for exe.dev. Use another Drukbox provider name for the
generic remote shape. The first pass writes `~/druks/druks.toml` with generated
secrets.

It creates `~/druks/.env` and exits if required values are missing.
The output identifies each missing value. Edit `druks.toml`. For a generic
remote shape, fill `[sandbox.<provider>]` from the Drukbox
[configuration reference](https://github.com/czpython/drukbox).

After boot, connect the GitHub App that Druks uses from **Settings → Services**.
Use the permission table in [Configuration](configuration.md#github).

The sandbox backend defaults to the local `docker` shape. On the first run, set
`DRUKS_PROVIDER` to select another shape. Use `exe` for exe.dev with Tailscale.
Use another Drukbox provider name for the generic remote shape. Druks passes this
name through without a provider registry.

Later runs read `[sandbox].provider`
from `druks.toml`. Thus, the environment flag only seeds a new installation.

If you want another installation directory, set
`DRUKS_INSTALL_DIR=/srv/druks`.

### 2. Re-run the installer

```bash
curl -fsSL https://druks.ai/install.sh | bash
```

The second pass creates `.env` from `druks.toml` and validates the required
values. Then it runs `docker compose pull`. It migrates the databases with
`docker compose run --rm web druks init-db`. A remote installation also migrates
the Drukbox schema. Finally, it runs `docker compose up -d`. Startup does not
run migrations.

### 3. Make sure that the stack operates

```bash
cd ~/druks
docker compose ps
curl -fsS http://127.0.0.1:8001/health
```

Then connect Anthropic and ChatGPT from **Settings → Providers → Connect**. Each card
opens the provider authorization page. Paste the returned code into the card.
Subscription tokens live in the database, not in a host CLI login. Agent runs
do not start with a disconnected harness.

After both connections:

```bash
docker compose exec web druks doctor
```

Each configured check must pass. You can run the command before you connect the
harnesses. In that state, both harness credential checks report errors.

### 4. Expose the public surfaces

**On exe.dev**, one port-share carries both dashboard and webhooks:

```bash
ssh exe.dev share port druks 8000
ssh exe.dev share set-public druks
```

The public provider webhooks use
`https://<host>/_external/{github,linear,jira}/events/`. The PAT-authenticated MCP
endpoint uses `https://<host>/mcp`. See
[Connect your agent](connect-your-agent.md). The dashboard uses
`https://<host>/`. exe.dev authenticates at the edge. Druks maps the asserted
email to your account.

**On another remote provider**, the dashboard uses your identity proxy. Set
`[identity].header` in `druks.toml` to the injected header. Webhook senders
cannot authenticate through SSO. They require a separate public HTTPS path.
The stack Caddy provides this path:

The proxy must strip any client-supplied copy of that identity header before
inserting its authenticated value. It must also terminate TLS and set HSTS.
Druks' shipped Caddy dashboard listener is loopback HTTP behind that edge.

Configure public webhook access:

1. Point an A record, such as `druks.example.com`, at the host.
2. Open inbound ports 80 and 443.
3. Set `[urls].webhook_host = "druks.example.com"` in `druks.toml`.
4. Run the installer again.

Caddy provisions a Let's Encrypt certificate for that hostname. It serves only
`POST /_external/*` and the PAT-authenticated `/mcp` endpoint. It does not serve
dashboard routes or the identity header. Thus, a public client cannot forge the
SSO gate.

Webhook URLs become
`https://druks.example.com/_external/<provider>/events/`. Agents connect at
`https://druks.example.com/mcp`
([Connect your agent](connect-your-agent.md)). Leave
`[urls].webhook_host` blank to bring your own ingress instead.

## Update / redeploy

Edit `druks.toml`. Then run the installer from the version that you will deploy.
The installer creates `.env` and refreshes `compose.yaml` and the Caddyfile. It
applies new migrations with `docker compose run --rm web druks init-db`. A remote
installation also migrates Drukbox.

Then the installer pulls the images and
starts the stack. Compose replaces only changed services. To migrate without
the installer, run `docker compose run --rm web druks init-db`.

Recreating `web` interrupts in-flight execution. DBOS recovers compatible
workflows from completed checkpoints when the process returns. Changes to
workflow structure, step order, step names, or serialized input can break
compatibility.

Before such a deployment, drain affected runs. You can also keep
an executor with compatible code until the runs finish. Recovery does not
preserve a live agent process inside a sandbox. It follows the operation
boundary in
[Concepts](concepts.md#durability-and-recovery).

### One-time: upgrading a box that ran the backend as root

The backend containers use the deployment user (`DRUKS_UID` and `DRUKS_GID`),
not root. A host from an older deployment can have root-owned files. The
non-root containers must write to `logs/` and `prompt-cache/` in the data
directory.

`install.sh` sets `DRUKS_UID` and `DRUKS_GID`. It also changes the
owner of the sandbox-keys volume. The script does not have root privileges. If
the host has root-owned files, run this command one time. If you changed
`DRUKS_DATA_HOST_DIR`, adjust the path:

```bash
docker run --rm -v "$HOME/druks-data:/d" alpine chown -R "$(id -u):$(id -g)" /d
```

The root container changes the owner of the bind-mounted host paths. The host
does not require `sudo`. Then run the normal upgrade with `install.sh`. It writes
`DRUKS_UID` and `DRUKS_GID`. It changes the owner of the sandbox-keys volume and
recreates the stack as the deployment user.

A new installation does not require
this step. `install.sh` and the deployment user own its files from the start.

`main` and `latest` are the edge channel. For a tagged installation, get the
installer from that tag. Set `DRUKS_REF` to the same tag. The installer selects
the related image tag. See [Releasing Druks](releasing.md) for the
immutable install shape.

## Rollback

Each Druks commit has an image tag in the form `:sha-<full-git-sha>`. The image
contains both the API and the SPA build. It is one release artifact.
Pin a specific build by setting `DRUKS_TAG` in `.env`:

```bash
DRUKS_TAG=sha-0123456789abcdef0123456789abcdef01234567 docker compose up -d
```

This command rolls back the image, not the database schema. `druks init-db`
only upgrades migrations. It does not downgrade them. Before you select an
older image, make sure that its code can read the current schema. Make sure that
it can read the current `druks.toml` and `.env`. Active workflows must also be
compatible with that code.

## Logs / stop

```bash
docker compose logs -f web
docker compose logs -f drukbox               # the sandbox control plane
docker compose down
```

## How the proxy routes

exe.dev exposes one port. Caddy enforces access for each path. It uses the stock
image, host network, port `:8000`, and the Caddyfile from the installer:

- **Webhooks:** Druks exposes `POST /_external/*` publicly. The matching webhook
  class authenticates each request. Per-provider paths land under
  `/_external/<provider>/<category>/`. App role-module discovery
  registers them at import time.
- **MCP:** Druks exposes `/mcp` publicly. A personal access token authenticates
  each request inside Druks. Caddy does not buffer this route, so its SSE frames stream.
- **Dashboard:** Everything else requires a nonempty trusted identity header. The exe.dev
  login supplies this header. Caddy proxies the request to `web` at
  `127.0.0.1:8001`. This service supplies the API, SPA, and app frontends. Druks
  maps the asserted email to your account for each request
  ([access control](configuration.md#public-urls-and-access-control)).
