---
title: "Full local setup"
description: "Run Druks, Postgres, Redis, Drukbox, and local sandbox containers on one machine."
icon: "laptop"
---

The local shape keeps every component on one machine:

```text
browser -> Druks :8001 -> Drukbox :8780 -> Docker sandbox containers
                         \
                          -> SSH from Druks to each container
```

Compose runs Druks, Postgres, Redis, and Drukbox. The Drukbox service holds the
Docker socket of the host. Its `docker` provider starts sandboxes as sibling
containers on the host daemon. Agent work stays in these isolated containers.
It does not run in the Druks process.

## Prerequisites

- Docker with the Compose plugin
- Sufficient local Docker capacity for Postgres, Redis, Druks, Drukbox, and
  short-lived sandbox containers.

You need no Tailscale account or remote VM provider.
The image registry provides Druks service and sandbox images for both
`linux/amd64` and `linux/arm64`.

## 1. Install the local Druks profile

```bash
curl -fsSL https://druks.ai/install.sh | bash
```

`docker` is the default provider. Thus, the bare command selects the local
shape. `DRUKS_PROVIDER=docker` makes the same selection explicit.

The local shape needs no authored values, so the first run goes all the way:

- The installer writes `~/druks/druks.toml` with `[sandbox].provider = "docker"`.
- It creates `~/druks/.env` with `DEFAULT_HOST_PROVIDER=docker`.
- It generates the database password and the stored-secret key.
- It pulls images and applies migrations.
- It starts Druks, Postgres, Redis, and Drukbox on `127.0.0.1:8780`.
- It uses `COMPOSE_FILE=compose.yaml:compose.override.yaml` without Caddy or the
  janitor profiles.

Drukbox controls sandboxes through the mounted `/var/run/docker.sock`. The
installer records the group ID of the socket in `.env`. This value gives the
non-root service user access to the socket. Drukbox keeps its schema in a
`drukbox` database in the same Postgres instance. It does not require a separate
data store. If sandbox SSH is unreachable on macOS, enable host networking in
the Docker Desktop settings.

For the bundled `software_factory` app, connect its GitHub App after startup.
Use **Settings → Services** in the dashboard. Create the app there, or paste the
credentials of an existing GitHub App. See
[the GitHub connection](configuration.md#github).

If an existing installation runs Drukbox through `make dev`, finish or cancel
the local runs. Stop the host process. Then run the installer again. The Compose
service starts with a new sandbox registry.

## 2. Make sure that the first system operates

```bash
cd ~/druks
docker compose ps
curl -fsS http://127.0.0.1:8001/health
```

Success means the Compose services are up and the health endpoint returns
`{"status":"ok"}`. The dashboard is at
[http://127.0.0.1:8001](http://127.0.0.1:8001).

## 3. Connect agent harnesses

Open **Settings → Harnesses** in the dashboard and connect Claude and Codex.
Druks stores those subscription credentials in Postgres and writes a fresh
credential file into each sandbox. It does not use host CLI login files.
The local profile uses `[identity].mode = "none"`. It has no browser
authentication and exactly one operator account.

A new installation shows its
setup page until the first harness connection completes. That connection
creates the operator account from the provider-verified email. Protect database
access and backups as credential data. Harness payloads do not use the
`[secrets].secrets_key` envelope that protects MCP tokens and OAuth grants.

Agent calls refuse before provisioning if their selected harness is not
connected. `druks doctor` reports the connection and token expiry for every
registered harness.

Run the complete preflight:

```bash
cd ~/druks
docker compose exec web druks doctor
```

Each configured check must be green. You can run this command before you connect
the harnesses. The Claude and Codex credential checks will fail in that state.

To prove the full sandbox path rather than only Drukbox's control-plane health:

```bash
docker compose exec web druks doctor --sandbox
```

This creates and deletes a real sandbox container.

## 4. Log in a browser session

Create the browser session:

1. Open **Settings → Browser sessions**.
2. Create a stable session name.
3. Choose **Log in**. Druks opens a headed browser in a disposable browser sandbox.
4. Authenticate on the site.
5. Choose **Save**. Druks closes the browser and stores its encrypted profile.
   It marks the session as ready.

When a site expires the login, the session becomes stale. Choose **Reconnect**.
Druks creates a new login window from the saved state. Authenticate again. Then
save the replacement profile.

**Cancel** deletes the disposable sandbox without
a change to the saved state. A web-process restart also deletes open login
windows. After Druks returns, open the window again.

To examine the complete path, save the session. Then run an app workflow that
borrows it. Make sure that its browser opens the authenticated site. A saved
login window always stores `profile_dir`. This rule also applies to a session
that came from Playwright `storage_state`.

## 5. Exercise an app

Druks does not invent a generic domain job. An installed app supplies the
workflow and its trigger. In the bundled distribution, `software_factory` is
the reference app. Register a project in its dashboard. Use its configured
ticket or GitHub trigger.

The run appears on the subject page and in the Events
feed. Agent-call pages stream transcript and artifact data.

If you develop a different app, install that distribution into a
development Druks environment and invoke its documented trigger or
`Workflow.start()` path. See [writing an app](writing-an-app.md).

## Sandbox image

`[sandbox].image` selects the image Drukbox starts. The shipped
`ghcr.io/czpython/druks/sandbox:latest` image contains the non-root `druks`
user plus Git, GitHub CLI, Node, Claude, and Codex.

If you change the sandbox, build the image from the repository:

```bash
docker build -t druks-sandbox deploy/sandbox
```

Set the image in `~/druks/druks.toml`, then re-run the installer:

```toml
[sandbox]
image = "druks-sandbox"
```

Existing hosts keep their original image. New acquisitions use the updated
value.

## Webhook caveat

GitHub, Linear, and Jira cannot connect to a loopback listener. Dashboard-initiated
actions work locally, but provider-driven flows need an HTTPS tunnel forwarding
to `127.0.0.1:8001`. Connect tracker credentials under **Settings → Services** and
keep the exact public paths:

```text
/_external/github/events/
/_external/linear/events/
/_external/jira/events/
```

The tunnel must preserve request bodies and signature headers. Do not expose
the rest of the local dashboard without adding an authentication edge.

For changing Druks itself, use the host-run development topology in
[Development](development.md) rather than repeatedly rebuilding the production
image.
