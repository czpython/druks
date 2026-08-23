# Full local setup

The local shape keeps every component on one machine:

```text
browser -> Druks :8001 -> Drukbox :8780 -> Docker sandbox containers
                         \
                          -> SSH from Druks to each container
```

Everything runs in Compose: Druks, Postgres, Redis, and Drukbox — the
Drukbox service holds the host's Docker socket, so its `docker` provider
starts sandboxes as sibling containers on the host daemon. Agent work still
runs in those isolated containers rather than in the Druks process.

## Prerequisites

- Docker with the Compose plugin
- enough local Docker capacity for Postgres, Redis, Druks, Drukbox, and
  short-lived sandbox containers

No Tailscale account or remote VM provider is needed.
The Druks service and sandbox images are published for both `linux/amd64`
and `linux/arm64`.

## 1. Install the local Druks profile

```bash
curl -fsSL https://druks.ai/install.sh | bash
```

`docker` is the default provider, so the bare command selects the local shape;
`DRUKS_PROVIDER=docker` is equivalent and explicit.

The local shape needs no authored values, so the first run goes all the way:

- writes `~/druks/druks.toml` with `[sandbox].provider = "docker"`
- renders `~/druks/.env` with `DEFAULT_HOST_PROVIDER=docker`
- generates the database password and the stored-secret key
- pulls images, applies migrations, and starts Druks, Postgres, Redis, and
  Drukbox on `127.0.0.1:8780` (`COMPOSE_FILE=compose.yaml:compose.override.yaml`
  with no profiles: no Caddy and no janitor)

Drukbox drives sandboxes through the mounted `/var/run/docker.sock`; the
installer records the socket's group id in `.env` so the service's non-root
user may use it. Drukbox keeps its schema in a `drukbox` database in the same
Postgres — no separate datastore. On macOS, if sandbox SSH turns out
unreachable, enable host networking in Docker Desktop's settings.

For the bundled `ship` app, connect the GitHub App druks acts as from
the dashboard after boot (**Settings → Services**) — create it there or
paste an existing GitHub App's credentials, see
[the GitHub connection](configuration.md#github).

Existing installs that still run Drukbox on the host via `make dev`: finish
or cancel local runs, stop the host process, and re-run the installer — the
Compose service starts with a fresh sandbox registry.

## 2. Verify the first working system

```bash
cd ~/druks
docker compose ps
curl -fsS http://127.0.0.1:8001/health
```

Success means the Compose services are up and the health endpoint returns
`{"status":"ok"}`. The dashboard is at <http://127.0.0.1:8001>.

## 3. Connect agent harnesses

Open **Settings → Harnesses** in the dashboard and connect Claude and Codex.
Druks stores those subscription credentials in Postgres and writes a fresh
credential file into each sandbox. It does not use host CLI login files.
The local profile runs with `[identity].mode = "none"` — no browser
authentication and exactly one operator account. A fresh install shows
onboarding until the first harness connection completes; that connection
creates the sole operator account from the provider-verified email. Protect
database access and backups as credential-bearing data; unlike MCP tokens and
OAuth grants, harness payloads do not use the `[secrets].secrets_key` envelope.

Agent calls refuse before provisioning if their selected harness is not
connected. `druks doctor` reports the connection and token expiry for every
registered harness.

Now run the complete preflight:

```bash
cd ~/druks
docker compose exec web druks doctor
```

Every configured check should be green. Running this before connecting the
harnesses is still useful, but its Claude and Codex credential checks will
correctly fail.

To prove the full sandbox path rather than only Drukbox's control-plane health:

```bash
docker compose exec web druks doctor --sandbox
```

This creates and deletes a real sandbox container.

## 4. Log in a browser session

Open **Settings → Browser sessions**, create a stable session name, and choose
**Log in**. Druks opens a headed browser in a disposable browser sandbox and
shows it in the dashboard. Authenticate on the site as you normally would,
then choose **Save**. Druks closes the browser, stores its encrypted profile,
and marks the session ready.

When a site expires the login, the session becomes stale. Choose **Reconnect**
to seed a fresh login window from the saved state, authenticate again, and
save the replacement profile. Cancel destroys the disposable sandbox without
changing the saved state. A web-process restart also destroys open login
windows; reopen the window after Druks returns.

To verify the complete path, run an app workflow that borrows the
session after saving and confirm its browser opens the authenticated site.
Saving a login window always stores `profile_dir`, including when the session
was originally imported as Playwright `storage_state`.

## 5. Exercise an app

Druks does not invent a generic domain job: an installed app supplies the
workflow and its trigger. In the bundled distribution, `ship` is the reference
app. Register a project in its dashboard and use its configured ticket
or GitHub trigger. Watch the run appear in the subject page and Events feed;
agent-call pages stream transcript and artifact data.

If you are developing a different app, install that distribution into a
development Druks environment and invoke its documented trigger or
`Workflow.start()` path. See [writing an app](writing-an-app.md).

## Sandbox image

`[sandbox].image` selects the image Drukbox starts. The shipped
`ghcr.io/czpython/druks-sandbox:latest` image contains the non-root `druks`
user plus Git, GitHub CLI, Node, Claude, and Codex.

Build it from the repository when changing the sandbox:

```bash
docker build -t druks-sandbox deploy/sandbox
```

Set the image in `~/druks/druks.toml`, then re-run the installer:

```toml
[sandbox]
image = "druks-sandbox"
```

Existing hosts keep their original image; new acquisitions use the updated
value.

## Webhook caveat

GitHub, Linear, and Jira cannot reach a loopback listener. Dashboard-initiated
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
