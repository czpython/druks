# Full local setup

The local profile keeps every component on one machine:

```text
browser -> Druks :8001 -> Drukbox :8000 -> Docker sandbox containers
                         \
                          -> SSH from Druks to each container
```

Druks, Postgres, and Redis run in Compose. Drukbox runs on the host because its
`docker` provider needs access to the host Docker daemon. Agent work still runs
in isolated containers rather than in the Druks process.

## Prerequisites

- Docker with the Compose plugin
- Git
- enough local Docker capacity for Postgres, Redis, Druks, Drukbox, and
  short-lived sandbox containers
- two GitHub Apps if you intend to use the bundled `build` extension

No Tailscale account or remote VM provider is needed.
The Druks application and sandbox images are published for both `linux/amd64`
and `linux/arm64`.

## 1. Install the local Druks profile

```bash
DRUKS_PROVIDER=docker bash <(curl -fsSL https://raw.githubusercontent.com/czpython/druks/main/scripts/install.sh)
```

The first run:

- writes `~/druks/.env` with `DEFAULT_HOST_PROVIDER=docker`
- generates the database, webhook, notification, and stored-secret keys
- points Druks at Drukbox on `127.0.0.1:8000`
- prints blank required fields and exits without booting if setup is incomplete

For the bundled `build` extension, provision its GitHub Apps:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/czpython/druks/main/scripts/install.sh) --apps
```

Then re-run the installer. A boot-ready run pulls images, applies Druks
migrations, and starts Druks, Postgres, and Redis. It does not start Drukbox in
the local profile.

## 2. Run Drukbox on the host

In a separate checkout:

```bash
git clone https://github.com/czpython/drukbox
cd drukbox
DOCKER_SSH_USERNAME=druks make dev
```

`make dev` starts Drukbox on `127.0.0.1:8000` with the Docker provider,
Tailscale disabled, and the `dev-token` expected by the local Druks setup.
`DOCKER_SSH_USERNAME=druks` matches the non-root user in the shipped sandbox
image.

If port 8000 is already occupied, run Drukbox on another port and update
`DRUKS_SANDBOX_SERVICE_URL` in `~/druks/.env`, then recreate the web service:

```bash
cd ~/druks
docker compose up -d --force-recreate web
```

## 3. Verify the first working system

```bash
cd ~/druks
docker compose ps
curl -fsS http://127.0.0.1:8001/health
```

Success means the Compose services are up and the health endpoint returns
`{"status":"ok"}`. The dashboard is at <http://127.0.0.1:8001>.

## 4. Connect agent harnesses

Open **Settings → Harnesses** in the dashboard and connect Claude and Codex.
Druks stores those subscription credentials in Postgres and writes a fresh
credential file into each sandbox. It does not use host CLI login files.
The local profile runs `DRUKS_AUTH_MODE=none` — no browser authentication and
exactly one operator account. A fresh install shows onboarding until the
first harness connection completes; that connection creates the sole operator
account from the provider-verified email. Protect database access and backups
as credential-bearing data; unlike MCP tokens and OAuth grants, harness
payloads do not use the `DRUKS_SECRETS_KEY` envelope.

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

## 5. Exercise an application

Druks does not invent a generic domain job: an installed extension supplies the
workflow and its trigger. In the bundled distribution, `build` is the reference
application. Register a project in its dashboard and use its configured ticket
or GitHub trigger. Watch the run appear in the subject page and Events feed;
agent-call pages stream transcript and artifact data.

If you are developing a different extension, install that distribution into a
development Druks environment and invoke its documented trigger or
`Workflow.start()` path. See [writing an extension](writing-an-extension.md).

## Sandbox image

`DRUKS_SANDBOX_IMAGE` selects the image Drukbox starts. The shipped
`ghcr.io/czpython/druks-sandbox:latest` image contains the non-root `druks`
user plus Git, GitHub CLI, Node, Claude, and Codex.

Build it from the repository when changing the sandbox:

```bash
docker build -t druks-sandbox deploy/sandbox
```

Set `DRUKS_SANDBOX_IMAGE=druks-sandbox` in `~/druks/.env` and recreate the web
service. Existing hosts keep their original image; new acquisitions use the
updated value.

## Webhook caveat

GitHub, Linear, and Jira cannot reach a loopback listener. Dashboard-initiated
actions work locally, but provider-driven flows need an HTTPS tunnel forwarding
to `127.0.0.1:8001`. Keep the exact public paths:

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
