# Configuration

Druks has two configuration planes. Use environment variables for process and
deployment topology. Use the dashboard for operator choices that should change
without replacing the process.

| Plane | Examples | Stored in |
| --- | --- | --- |
| Environment | database, Redis, ingress, GitHub App keys, Drukbox, encryption key | `.env` / process environment |
| Dashboard | timezone, harness connections/defaults, workflow and agent overrides, notifications, MCP servers, skills | Postgres |

The [`Settings` model](../backend/druks/settings.py) is the authority for
environment variables. [`.env.example`](../.env.example) is a development
template; `druks setup` writes the larger deployment `.env` used by Compose and
Drukbox.

## Core process settings

| Variable | Default | Purpose |
| --- | --- | --- |
| `DRUKS_DATABASE_URL` | local `druks` Postgres | Application and DBOS database |
| `DRUKS_REDIS_URL` | `redis://127.0.0.1:6379/0` | Short-lived coordination and caches |
| `DRUKS_DATA_DIR` | `/var/lib/druks` | Logs, artifacts, installed skills |
| `DRUKS_LOG_LEVEL` | `INFO` | Python and DBOS log level |
| `DRUKS_SECRETS_KEY` | none; required | MCP/OAuth secret encryption keys |

Postgres is durable state. Redis is not the workflow state store: it supports
short-lived concerns including webhook delivery claims, OAuth state and token
caches, and the sandbox provisioning gate.

## Public URLs and access control

| Variable | Purpose |
| --- | --- |
| `DRUKS_ENDPOINT` | Browser-visible dashboard base URL used to build MCP OAuth callbacks |
| `DRUKS_WEBHOOK_HOST` | Public webhook hostname used by `druks doctor` for its ingress probe |
| `DRUKS_WEBHOOK_SECRET` | Shared HMAC secret used by bundled webhook integrations |
| `DRUKS_AUTH_HEADER` | Identity header the edge (Caddy) requires; the app never reads it |

`DRUKS_ENDPOINT` and `DRUKS_WEBHOOK_HOST` are different. The first is where an
operator's browser reaches Druks; the second is the public ingress webhook
senders reach. They may share a hostname on exe.dev.

Harness login is the account door: signing in to Codex or Claude from the
dashboard resolves your account and mints the `druks_session` cookie
(HttpOnly, 30-day sliding TTL in Redis) that every internal API and SSE
stream requires. The shipped remote Caddy admits only requests carrying a
nonempty `DRUKS_AUTH_HEADER` identity — pure admission; the app never reads
the header. Public `POST /_external/*` routes bypass the
edge identity check and carry their own authentication — webhook signature
verification, and the notification respond route's correlation token. Do not
publish the local `127.0.0.1:8001` listener directly. Configure
the identity proxy to strip every client-supplied copy of
`DRUKS_AUTH_HEADER` — a client that can inject it walks past the edge. Terminate
TLS and set HSTS at that public proxy; the shipped Caddy listener is loopback
HTTP behind the TLS edge.

## Personal access tokens

Agents and other non-browser clients authenticate the same internal API with
personal access tokens minted in Settings → Agent access, sent as
`Authorization: Bearer <token>`. A token serializes as
`druks_pat_<prefix>_<secret>`; Druks stores only the SHA-256 of the full
token, shows the plaintext exactly once at mint, and expires it 365 days
after creation. When the header is present it must authenticate — a bad
token is a 401, never a fall back to the session cookie — and token
management itself accepts the dashboard session only, so a leaked token
cannot mint or revoke tokens. On compromise, revoke the token in Settings →
Agent access (immediate; the list shows each token's prefix and last use,
tracked hourly, to identify it) and mint a replacement — rotation is mint
first, revoke second. Agents consume the API through the MCP endpoint; see
[Connect your agent](connect-your-agent.md).

## GitHub Apps

The bundled `build` extension requires two GitHub Apps. These are application
requirements, not requirements of the Druks extension mechanism itself.

- **Operator app:** receives webhooks and performs application-owned writes
  such as branches, pull requests, comments, labels, and merges.
- **Reviewer app:** submits reviews through a distinct GitHub identity.

Personal access tokens are not a supported substitute. Install both Apps on
the same repositories; that installation set is where `build` may act.

The fast path is:

```bash
cd ~/druks
bash <(curl -fsSL https://raw.githubusercontent.com/czpython/druks/main/scripts/install.sh) --apps
```

This uses the GitHub manifest files under
[`scripts/manifests/`](../scripts/manifests) and writes the returned App ids,
PEMs, and webhook secret into the install.

### Operator app

```dotenv
GITHUB_OPERATOR_APP_ID=123456
GITHUB_OPERATOR_PRIVATE_KEY_PATH=/secrets/github_operator.pem
DRUKS_WEBHOOK_SECRET=<same secret configured on the app webhook>
```

Webhook URL:
`https://<webhook-host>/_external/github/events/`

Subscribe to pull request, pull request review, and push events.

| Repository permission | Access |
| --- | --- |
| Metadata | Read |
| Contents | Read and write |
| Pull requests | Read and write |
| Issues | Read and write |
| Checks | Read |
| Commit statuses | Read |

### Reviewer app

```dotenv
GITHUB_REVIEWER_APP_ID=123457
GITHUB_REVIEWER_PRIVATE_KEY_PATH=/secrets/github_reviewer.pem
```

It needs read access to metadata and contents and read/write access to pull
requests. It does not need a webhook.

`GITHUB_API_URL` defaults to `https://api.github.com` and can point both clients
at another compatible GitHub API endpoint.

## Ticketing integrations

The bundled integrations support Linear and Jira. Configure only one ticketing
source for `build` intake.

Linear:

```dotenv
LINEAR_API_KEY=
LINEAR_WEBHOOK_SECRET=
```

Jira Cloud:

```dotenv
JIRA_BASE_URL=https://example.atlassian.net
JIRA_EMAIL=operator@example.com
JIRA_API_TOKEN=
JIRA_WEBHOOK_SECRET=
```

`druks doctor` treats an entirely absent integration as optional. Once the main
credential fields are present, its webhook secret is required. Webhook URLs use
`/_external/linear/events/` and `/_external/jira/events/`.

The statuses that trigger or move `build` work are settings declared by the
extension and edited in the dashboard, not environment variables.

## Harnesses

Claude and Codex subscription credentials are connected from **Settings →
Harnesses**. The login flow stores each credential in Postgres; Druks refreshes
it on a schedule and synthesizes the CLI credential file inside each sandbox.
It does not copy a host login.

Process settings such as `DRUKS_CLAUDE_CONFIG_DIR` and
`DRUKS_CODEX_CONFIG_DIR` point at optional non-auth CLI configuration to carry
into sandboxes. The Compose deployment mounts these read-only. Harness defaults
and per-agent model, effort, and timeout overrides live in dashboard settings.
A call refuses before provisioning a VM if its selected harness is not
connected.

## Sandboxes

| Variable | Purpose |
| --- | --- |
| `DRUKS_SANDBOX_SERVICE_URL` | Drukbox API base URL; empty disables sandbox-backed execution |
| `DRUKS_SANDBOX_SERVICE_TOKEN` | Drukbox API token |
| `DRUKS_SANDBOX_SERVICE_TIMEOUT` | Control-plane request timeout; default 180 seconds |
| `DRUKS_SANDBOX_IMAGE` | Optional provider image override |
| `DRUKS_SANDBOX_KEYS_DIR` | Per-host SSH private-key directory |

`druks setup` also writes the selected Drukbox provider settings. The installer
profiles are `exe`, `aws`, and `docker`; provider-specific credentials and host
options are interpreted by Drukbox. See [deployment](../deploy/README.md) or
[full local setup](full-local.md) for the topology.

## Notifications

Destinations are managed from the dashboard. The current destination kind is a
Slack incoming webhook. Actionable messages use Slack Block Kit;
non-actionable messages use the same URL through Apprise.
`SLACK_SIGNING_SECRET` authenticates Slack interactivity callbacks.

Choose one enabled destination as the gate-notification destination in
Settings. A parked subjected run then produces a durable notification. Failure
to deliver the notification does not unpark or fail the run.

## MCP servers

`DRUKS_MCP_CATALOG` points at a JSON catalog of server definitions loaded at
startup. The packaged catalog declares Linear OAuth but leaves it disabled; a
deployment may replace the catalog. Catalogs contain definitions, not tokens.

`DRUKS_MCP_TRUSTED` points at the trust-pins JSON behind the registry
resolver's official badge. The badge is computed: an entry is official when
its publisher namespace, reversed into a domain, matches the remote endpoint's
host (`com.grafana` publishing on `*.grafana.com` self-certifies). Pins cover
the two gaps the rule cannot derive, one `name: value` line each, told apart
by the value's shape:

- a publisher namespace (`"grafana": "io.github.grafana"`) vouches for a
  publisher the rule cannot match; the entry's url stays live from the
  registry.
- an `http…` url (`"sentry": "https://mcp.sentry.dev/mcp"`) supplies the
  hosted endpoint the registry entry omits entirely.

To decide which to write: if the registry entry already declares the hosted
url, pin the publisher; if it lacks one, pin the url.

The dashboard can enable catalog entries and add custom servers. Authentication
is one of:

- static token stored encrypted in Postgres
- token read from a named process environment variable
- OAuth connection, which requires `DRUKS_ENDPOINT`

Enabled servers are delivered to both harnesses unless an extension workspace
owns a required server with the same name. Tokens enter the agent environment
under a derived variable and are never returned by the API.

## Skills

The dashboard installs skill collections from GitHub repositories.
`DRUKS_SKILLS_DIR` selects the shared writable directory; otherwise it defaults
to `<DRUKS_DATA_DIR>/skills`. Enabled skills are copied into both CLI homes in
each sandbox. Disabled skills are excluded from the upload and from the
per-agent capability manifest.

## Credential custody and secrets at rest

`DRUKS_SECRETS_KEY` encrypts MCP tokens and OAuth grants with AES-256-GCM.
Each database column supplies authenticated associated data, and each value
gets a derived encryption key. The setting is one or more comma-separated,
base64-encoded 32-byte master keys:

```bash
python3 -c 'import base64, os; print(base64.b64encode(os.urandom(32)).decode())'
```

The first key encrypts new values; every listed key may decrypt. To rotate,
prepend a new key:

```dotenv
DRUKS_SECRETS_KEY=<new>,<old>
```

Keep the old key until no stored row depends on it. Losing every key used for a
row makes that secret unrecoverable; reconnect OAuth grants and re-enter static
tokens. Validation and API errors intentionally omit submitted secret values.

The encryption envelope does **not** currently cover harness subscription
payloads or notification webhook URLs. They are stored as ordinary Postgres
fields, although APIs withhold or mask their values. Treat access to Postgres
and its backups as access to those credentials. GitHub App private keys remain
files mounted into the process rather than database values.
