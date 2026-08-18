# Configuration

Druks has two authored configuration planes. Use `druks.toml` for process and
deployment topology. Use the dashboard for operator choices that should change
without replacing the process.

| Plane | Examples | Stored in |
| --- | --- | --- |
| Deployment | identity, ingress, Drukbox, encryption key | `~/druks/druks.toml` |
| Dashboard | timezone, the GitHub connection, harness and tracker credentials, workflow and agent overrides, notifications, MCP servers, skills | Postgres |

The installer renders the complete deployment `.env` from `druks.toml`; `.env`
is a build artifact consumed by Compose, Druks, and Drukbox, not an authored
configuration file. Edit `druks.toml` and re-run the installer to render and
apply changes. Running `druks setup` alone re-renders `.env` but does not
restart services.

Format follows habitat: repository-committed files such as ship's
`.druks/ship/config.yml` are YAML like the rest of the repository-dotfile
world; box-resident operator files are TOML because they render to env
byte-exact. The two files share no keys and no reader.

`druks.toml` is the authority for authored process configuration. Environment
variables are reserved for Compose-injected infrastructure such as database,
Redis, data, and container paths. [`.env.example`](../.env.example) is the
host-run development template for that environment plane.

## Deployment file

`druks.toml` has one table per operator concern:

| Table | Purpose |
| --- | --- |
| `[identity]` | Browser identity mode and header or JWT verification inputs |
| `[urls]` | Dashboard callback base URL and public webhook hostname |
| `[secrets]` | Generated deployment secrets |
| `[paths]` | Host data and harness configuration paths |
| `[sandbox]` | Drukbox provider, service URL, token behavior, and image override |
| `[sandbox.<provider>]` | Provider environment passed through to the remote stack |
| `[env]` | Additional deployment environment settings rendered verbatim |

A blank string is unset and is omitted from `.env`. Use `[env]` for settings
without another `druks.toml` home, including additional `DRUKS_*` settings. A
key already owned by the renderer is reported as a configuration gap instead
of overriding its canonical value. On a remote shape,
`[sandbox.<provider>]` accepts the variables documented by
[Drukbox](https://github.com/czpython/drukbox); Druks does not enumerate
providers. `docker` and `exe` select shape-specific first-write templates.
Every other provider name selects the generic remote shape and is validated by
Drukbox.
The local `docker` shape does not render `[sandbox.<provider>]`: its Drukbox
service takes a fixed environment from `deploy/compose.local.yaml`.

Secrets are generated only when the TOML is first created. Preserve
`[secrets]` when moving or recovering an installation. Use repeatable
`druks setup ... --set key.path=value` arguments for explicit scripted writes.

## Core process settings

| Variable | Default | Purpose |
| --- | --- | --- |
| `DRUKS_DATABASE_URL` | local `druks` Postgres | Application and DBOS database |
| `DRUKS_TEST_DATABASE_URL` | local `druks_test` Postgres | What the shipped pytest fixtures use — never the application's |
| `DRUKS_TEST_REDIS_URL` | `redis://127.0.0.1:6379/15` | What the shipped pytest fixtures flush |
| `DRUKS_REDIS_URL` | `redis://127.0.0.1:6379/0` | Short-lived coordination and caches |
| `DRUKS_DATA_DIR` | `/var/lib/druks` | Logs, artifacts, installed skills |
| `DRUKS_LOG_LEVEL` | `INFO` | Python and DBOS log level |

Postgres is durable state. Redis is not the workflow state store: it supports
short-lived concerns including webhook delivery claims, OAuth state and token
caches, and the sandbox provisioning gate.

## Public URLs and access control

| TOML key | Purpose |
| --- | --- |
| `urls.endpoint` | Browser-visible dashboard base URL used to build MCP OAuth callbacks |
| `urls.webhook_host` | Public webhook hostname used by `druks doctor` for its ingress probe |
| `identity.mode` | `none` (default; no authentication, single operator), `header` (edge-asserted identity), or `jwt` (edge-signed assertion, verified) |
| `identity.header` | The trusted identity header; rendered for the shipped Caddy edge too. No default — header and jwt modes refuse to start without it |
| `identity.jwks_url` | `jwt` mode: where the edge publishes its signing keys |
| `identity.jwt_issuer` | `jwt` mode: required `iss` claim value |
| `identity.jwt_audience` | `jwt` mode: required `aud` claim value |
| `identity.jwt_identity_claim` | `jwt` mode: the claim mapped to the account (default `email`) |

`urls.endpoint` and `urls.webhook_host` are different. The first is where an
operator's browser reaches Druks; the second is the public ingress webhook
senders reach. They may share a hostname on exe.dev.

Druks does not authenticate browsers. Identity resolves per request, in this
order:

1. **Personal access token.** When an `Authorization` header is present it
   must authenticate — a malformed or dead bearer is a 401, never a fall
   through to the modes below.
2. **`header` mode.** The edge (exe.dev, Teleport, Cloudflare Access, …)
   authenticates and asserts the operator's email as exactly one nonblank
   `identity.header` value; Druks trims outer whitespace and maps it to an
   account, creating one on first sight (open enrollment — the edge decides
   who reaches Druks at all; the account column is case-insensitive).
3. **`jwt` mode.** The same assertion channel as `header` mode, but the value
   is a signed JWT: Druks verifies the RS256 signature against
   `identity.jwks_url` (keys cached for five minutes and refetched on
   rotation), requires `exp`, `iss`, and `aud` to match the configured
   issuer and audience, and maps the verified
   `identity.jwt_identity_claim` through the same open enrollment. A
   failed verification is a 401 naming only the failure class — never the
   token. Confirm the real edge's header name, claims, and rotation story
   before enabling the mode; the RS256 profile is pinned, not negotiated.
4. **`none` mode.** No authentication and no identity edge: Druks resolves
   the only non-system account. Zero accounts is the setup state — the
   dashboard onboards by connecting a harness, and the first completed
   connection creates the operator account from the provider-verified email.
   More than one non-system account is configuration drift: Druks refuses
   requests (and startup) loudly rather than guess.

Trust requirements for `header` mode: the edge must authenticate every
dashboard request, must strip any client-supplied copy of the configured
identity header before inserting its authenticated value — a client that can
inject the header can be anyone — and must terminate TLS and set HSTS.
`jwt` mode keeps the same strip requirement but adds cryptographic
provenance: a forged header value fails signature verification instead of
becoming an identity, so a misconfigured proxy degrades to a 401 rather
than an impersonation.
The shipped Caddy listener is loopback HTTP behind that edge, and the Druks
web listener itself binds loopback by default. In `none` mode there is no
authentication at all, so the listener must stay loopback-only — never
publish it.

Any public listener that bypasses the identity edge must never forward the
configured identity header upstream. The shipped webhook listener already
serves only provider-authenticated `/_external/*` and the PAT-authenticated `/mcp` —
nothing that resolves the header — and any future public listener (for example
the planned MCP integrations listener) must keep that same isolation.

Public `POST /_external/*` routes bypass the identity gate and carry their
own authentication — webhook signature verification, and the notification
respond route's correlation token. `GET /api/auth/me` answers without a
resolved account so the dashboard can render onboarding in the `none`-mode
setup state.

## Personal access tokens

Agents and other non-browser clients authenticate the same internal API with
personal access tokens minted in Settings → Tokens, sent as
`Authorization: Bearer <token>`. A token serializes as
`druks_pat_<prefix>_<secret>`; Druks stores only the SHA-256 of the full
token, shows the plaintext exactly once at mint, and expires it 365 days
after creation. When the header is present it must authenticate — a bad
token is a 401, never a fall back to edge identity — and token management
itself accepts the signed-in identity only (edge-asserted, or the none-mode
operator) and refuses any `Authorization` header, so a leaked token cannot
mint or revoke tokens. On compromise, revoke the token in Settings → Tokens
(immediate; the list shows each token's prefix and last use, tracked hourly,
to identify it) and mint a replacement — rotation is mint first, revoke
second. Agents consume the API through the MCP endpoint; see
[Connect your agent](connect-your-agent.md).

## GitHub

Druks acts at GitHub as one **operator App** — its service identity. The App
receives webhooks and performs application-owned writes such as branches,
pull requests, comments, labels, and merges. Its credentials live encrypted
in Postgres; there is no TOML, environment, or PEM-file source — until GitHub
is connected, agent runs refuse with a pointed message and `druks doctor`
reports the identity as not connected.

Connect it from **Settings → Services**. **Create GitHub App** registers the
App through GitHub's manifest flow: name a GitHub org (or leave it empty for
a personal account), confirm on GitHub, and druks stores the created App's
credentials and sends you on to install it on your repositories. Creating the
App needs `urls.endpoint` set to the base URL the operator's browser reaches
druks at, and the webhook lands on `urls.webhook_host` when configured, the
endpoint host otherwise.

Alternatively paste an existing App's credentials into the same card: the App
ID, the PEM private key exactly as GitHub issued it, and the webhook secret.
Connecting validates the pasted credentials against GitHub and stores the
App's slug; from then on every operator client resolves from that row and
webhook deliveries verify against its stored secret.

Registering the App by hand instead:

Webhook URL:
`https://<webhook-host>/_external/github/events/`

Subscribe to issue comment, pull request, pull request review, and push events.

| Repository permission | Access |
| --- | --- |
| Metadata | Read |
| Contents | Read and write |
| Pull requests | Read and write |
| Issues | Read and write |
| Checks | Read |
| Commit statuses | Read |

Install the App on the repositories Druks should work in; that installation
set is where `ship` may act. Personal access tokens are not a supported
substitute.

**Upgrading an existing installation** is a one-time paste on each live box
after rollout: open Settings → Services and connect GitHub with the existing
operator App's ID, private key, and webhook secret. Do not create a
replacement App — the current App's webhook and installations keep working
under the pasted credentials.

### Review identity (optional)

The bundled `review` extension can post its verdict reviews as a second
GitHub App, so GitHub accepts approvals on Druks-authored pull requests.
Configure it in **Settings → Review**: the review App ID and its PEM private
key, both stored encrypted and empty-as-unset. Leave the pair empty and
reviews publish as operator comments; setting both flips reviews to distinct
approving reviews. The review App needs read access to metadata and contents,
read/write access to pull requests, and no webhook.

`GITHUB_API_URL` defaults to `https://api.github.com` and can point every
client at another compatible GitHub API endpoint.

## Ticketing integrations

Tracker credentials are service identities: connect Linear (API key + webhook
secret) or Jira Cloud (base URL, email, API token, webhook secret) from
**Settings → Services**, on the same cards as the GitHub App. Connecting
verifies the credentials against the tracker before anything is stored. Which
tracker drives `ship` work — and the statuses that trigger or move it — stays a
ship extension setting in **Settings → Ship**.

Webhook URLs remain `/_external/linear/events/` and
`/_external/jira/events/`. The Jira webhook is a Jira Automation "Send web
request" action with **Issue data (Jira format)** as its body — the REST issue
JSON under `issue` is the one accepted shape — and the shared token in the
`x-druks-webhook-token` header. `druks doctor` treats a disconnected tracker as
optional, and reports pending setup when the selected tracker's identity is not
connected.

## Harnesses

Claude and Codex subscription credentials are connected from **Settings →
Harnesses**. The connect flow stores each credential in Postgres; Druks
refreshes it on a schedule and synthesizes the CLI credential file inside each
sandbox. It does not copy a host login. Connecting is a capability connect for
the requesting account — in a fresh `none`-mode install the first completed
connection also creates the operator account (see
[access control](#public-urls-and-access-control)).

Process settings such as `DRUKS_CLAUDE_CONFIG_DIR` and
`DRUKS_CODEX_CONFIG_DIR` point at optional non-auth CLI configuration to carry
into sandboxes. The Compose deployment mounts these read-only. Harness defaults
and per-agent model, effort, and timeout overrides live in dashboard settings.
A call refuses before provisioning a VM if its selected harness is not
connected.

## Sandboxes

| TOML key | Purpose |
| --- | --- |
| `sandbox.service_url` | Drukbox API base URL; empty disables sandbox-backed execution |
| `sandbox.service_token` | Drukbox API token |
| `sandbox.timeout` | Control-plane request timeout; default 180 seconds |
| `sandbox.image` | Optional provider image override |
| `sandbox.browser_login_proxy` | Login-window egress proxy; empty keeps the box IP |

`DRUKS_SANDBOX_KEYS_DIR` remains a process environment override for the
per-host SSH private-key directory.

`[sandbox].browser_login_proxy` routes the browser **login window** through an
HTTP proxy, so the login egresses from a different IP than the box — for sign-in
flows that reject a login from the box's own address. It applies to the login
window only — borrows keep the box IP, which is enough once the session is
minted. The value is an authless proxy address, e.g. `http://172.17.0.1:8888`;
credentials, if any, are terminated deploy-side, so no proxy secret enters
Druks. Two ways to stand an exit up: run a CONNECT proxy on an always-on device
reachable over the tailnet, or run a local `gost` relay
(`gost -L=http://172.17.0.1:8888 -F=http://USER:PASS@host:PORT`) in front of a
proxy you provide. Leaving it empty keeps today's behavior. A fixed proxy fails
closed — if the exit is unreachable the login browser errors rather than falling
back to the box IP.

`[sandbox].provider` accepts any Drukbox provider name. `docker` selects the
local install shape, `exe` selects the exe.dev + tailnet shape, and every other
name selects the generic remote shape. Provider-specific credentials and host
options live in `[sandbox.<provider>]` and are interpreted by Drukbox. See
[deployment](../deploy/README.md) or [full local setup](full-local.md) for the
topology.

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
- OAuth connection, which requires `urls.endpoint`

Enabled servers are delivered to both harnesses unless an extension workspace
owns a required server with the same name. Tokens enter the agent environment
under a derived variable and are never returned by the API.

## Skills

The dashboard installs skill collections from GitHub repositories.
`DRUKS_SKILLS_DIR` selects the shared writable directory; otherwise it defaults
to `<DRUKS_DATA_DIR>/skills`. A call receives the enabled skills it requests, or
every enabled skill when it requests none; build requests the repo profile's
recommended set. Other installed skills are excluded from the upload, and the
per-agent capability manifest records the delivered set.

## Credential custody and secrets at rest

`secrets.secrets_key` encrypts MCP tokens, OAuth grants, browser-session
payloads, and the GitHub service identity's private key and webhook secret
with AES-256-GCM.
Each database column supplies authenticated associated data, and each value
gets a derived encryption key. The setting is one or more comma-separated,
base64-encoded 32-byte master keys:

```bash
python3 -c 'import base64, os; print(base64.b64encode(os.urandom(32)).decode())'
```

The first key encrypts new values; every listed key may decrypt. To rotate,
prepend a new key in `druks.toml`, then re-run the installer:

```toml
[secrets]
secrets_key = "<new>,<old>"
```

Keep the old key until no stored row depends on it. Losing every key used for a
row makes that secret unrecoverable; reconnect OAuth grants, re-enter static
tokens, and log in to affected browser sessions again. Validation and API errors intentionally omit submitted secret values.

The encryption envelope does **not** currently cover harness subscription
payloads or notification webhook URLs. They are stored as
ordinary Postgres fields, although APIs withhold or mask their values. Treat
access to Postgres and its backups as access to those credentials. GitHub App
private keys — the operator identity's and the review extension's — are
database values under the envelope, no longer files mounted into the process.
