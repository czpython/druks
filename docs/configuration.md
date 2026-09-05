---
title: "Configuration"
description: "Configure deployment settings, access control, harnesses, services, sandboxes, notifications, MCP servers, and skills."
icon: "settings"
---

Druks has two authored configuration planes. Use `druks.toml` for process and
deployment topology. Use the dashboard for operator choices that can change
without replacing the process.

| Plane | Examples | Stored in |
| --- | --- | --- |
| Deployment | identity, ingress, Drukbox, encryption key | `~/druks/druks.toml` |
| Dashboard | timezone, the GitHub connection, harness and tracker credentials, workflow and agent overrides, notifications, MCP servers, skills | Postgres |

The installer creates the deployment `.env` from `druks.toml`. Compose, Druks,
and Drukbox consume this build artifact. Do not edit `.env`. Edit `druks.toml`,
then run the installer again to apply changes. `druks setup` creates `.env` but
does not restart services.

The file location determines its format. Repository files such as
`.druks/software_factory/config.yml` use YAML. Other repository dotfiles use
the same format. Operator files on the host use TOML because the installer must
create the environment without value changes. These files share no keys or
readers.

`druks.toml` is the authority for authored process configuration. Druks reserves
environment variables for infrastructure that Compose injects, such as database,
Redis, data, and container paths.
[`.env.example`](https://github.com/czpython/druks/blob/main/.env.example) is the
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
| `[registry]` | Registry host and credentials for private image access |
| `[templates]` | Shared repository path for sandbox template images |
| `[env]` | Additional deployment environment settings rendered verbatim |

A blank string means unset, and the renderer omits it from `.env`. Use `[env]` for settings
without another `druks.toml` home, including additional `DRUKS_*` settings. The
renderer reports a key that it already owns as a configuration gap instead
of overriding its canonical value.

On a remote shape,
`[sandbox.<provider>]` accepts the variables documented by
[Drukbox](https://github.com/czpython/drukbox). Druks does not enumerate
providers. `docker` and `exe` select shape-specific first-write templates.
Every other provider name selects the generic remote shape. Drukbox validates it.

The local `docker` shape does not render `[sandbox.<provider>]`. Its Drukbox
service gets its environment from the defaults in `deploy/compose.yaml`.

The installer generates secrets only when it first creates the TOML. When you move or
recover an installation, preserve `[secrets]`. Use repeatable
`druks setup ... --set key.path=value` arguments for explicit scripted writes.

## Core process settings

| Variable | Default | Purpose |
| --- | --- | --- |
| `DRUKS_DATABASE_URL` | local `druks` Postgres | Runtime and DBOS database |
| `DRUKS_TEST_DATABASE_URL` | local `druks_test` Postgres | What the shipped pytest fixtures use — never the runtime's |
| `DRUKS_TEST_REDIS_URL` | `redis://127.0.0.1:6379/15` | What the shipped pytest fixtures flush |
| `DRUKS_REDIS_URL` | `redis://127.0.0.1:6379/0` | Short-lived coordination and caches |
| `DRUKS_DATA_DIR` | `/var/lib/druks` | Logs, artifacts, installed skills |
| `DRUKS_LOG_LEVEL` | `INFO` | Python and DBOS log level |

Postgres stores durable state. Redis does not store workflow state. It supports
short-lived concerns including webhook delivery claims, OAuth state and token
caches, and the sandbox provisioning gate.

## Public URLs and access control

| TOML key | Purpose |
| --- | --- |
| `urls.endpoint` | Browser-visible dashboard base URL used to build MCP OAuth callbacks |
| `urls.webhook_host` | Public webhook hostname used by `druks doctor` for its ingress probe |
| `identity.mode` | `none` (default, no authentication, single operator), `header` (edge-asserted identity), or `jwt` (validated edge-signed assertion) |
| `identity.header` | The trusted identity header. The shipped Caddy edge also uses it. Header and JWT modes have no default and require it |
| `identity.jwks_url` | `jwt` mode: where the edge publishes its signing keys |
| `identity.jwt_issuer` | `jwt` mode: required `iss` claim value |
| `identity.jwt_audience` | `jwt` mode: required `aud` claim value |
| `identity.jwt_identity_claim` | `jwt` mode: the claim mapped to the account (default `email`) |

The `urls.webhook_host` listener binds every interface. A second TLS
terminator on the same box, for example `tailscale serve` on the tailnet
address, collides with it on port 443. One of the two stops. To keep the
other addresses free, set `DRUKS_WEBHOOK_BIND_HOST` in `[env]` to the public
address. Caddy then serves only that address. To keep IPv6, list the IPv4
and the IPv6 addresses.

`urls.endpoint` and `urls.webhook_host` are different. The first is where an
operator's browser reaches Druks. The second is the public ingress host for
webhook senders. They can share a hostname on exe.dev.

Druks does not authenticate browsers. Identity resolves per request, in this
order:

1. **Personal access token.** If an `Authorization` header is present, it must
   authenticate. A malformed or inactive bearer returns a 401. Druks does not
   continue to another mode.
2. **Header mode (`header`).** The edge authenticates the operator. The edge can be
   exe.dev, Teleport, or Cloudflare Access. It supplies exactly one nonblank
   `identity.header` value. Druks removes outer whitespace and maps the value to
   an account. Druks creates the account at first access.

   The edge controls who
   can access Druks. Account values are case-insensitive.
3. **JWT mode (`jwt`).** This mode uses the assertion channel from `header` mode, but
   its value is a signed JWT. Druks validates the RS256 signature against
   `identity.jwks_url`. It caches keys for five minutes and gets new keys after
   rotation.

   The `exp`, `iss`, and `aud` claims must match the configuration.
   Druks maps `identity.jwt_identity_claim` to an account. A validation error
   returns a 401 with the error class, not the token. Druks uses a fixed RS256
   profile and does not negotiate it.
4. **No-authentication mode (`none`).** This mode has no authentication or identity edge. Druks
   resolves the only non-system account. Zero accounts is the setup state. The
   first completed provider connection creates the operator account from the
   provider-validated email.

   More than one non-system account is configuration
   drift. Druks refuses requests and startup in this state.

A subscription is always one person's. An API key is the installation's:
one per provider, owned by no account, and visible to every account in
**Settings → Providers** with the name of the person who last pasted it. A
paste from any account replaces it.

Before you enable `jwt` mode, make sure that the edge uses the configured header,
claims, and rotation process.

The edge in `header` mode must authenticate each dashboard request. It must
remove client-supplied copies of the configured identity header. Then it must
put the authenticated value in the header. Otherwise, a client can select an
identity. The edge must also terminate TLS and set HSTS.

`jwt` mode has the same header-removal requirement. It also adds cryptographic
provenance. A forged value fails signature validation and returns a 401. Thus,
a bad proxy configuration does not create an impersonated identity.

The shipped Caddy listener is loopback HTTP behind that edge, and the Druks
web listener itself binds loopback by default. In `none` mode there is no
authentication. Keep the listener on loopback. Never publish it.

A public listener that bypasses the identity edge must not forward the
configured identity header. The shipped webhook listener serves only
provider-authenticated `/_external/*` and PAT-authenticated `/mcp` routes. These
routes do not resolve the header. A future public listener must keep the same
isolation.

Public `POST /_external/*` routes bypass the identity gate and use their own
authentication. Webhooks use signature validation. The notification response
route uses its correlation token. `GET /api/auth/me` answers without a
resolved account so the dashboard can render onboarding in the `none`-mode
setup state.

## Personal access tokens

Agents and other non-browser clients use personal access tokens for the
internal API. Mint these tokens in Settings → Tokens. Send a token as
`Authorization: Bearer <token>`. A token has the form
`druks_pat_<prefix>_<secret>`. Druks stores only the SHA-256 hash of the full
token. It shows the plaintext one time and expires the token after 365 days.

If the header is present, it must authenticate. A bad token returns a 401.
Druks does not use edge identity as a fallback. Token management accepts only a
signed-in identity. It refuses requests that contain an `Authorization` header.
Thus, a leaked token cannot mint or revoke tokens.

If someone compromises a token, mint a replacement. Then revoke the old token in
Settings → Tokens. Revocation is immediate. The list shows the prefix and last
use of each token.

Druks updates last use each hour. Agents consume the API
through the MCP endpoint. See
[Connect your agent](connect-your-agent.md).

## GitHub

Druks acts at GitHub as one **operator GitHub App**. This app is its service
identity. The GitHub App receives webhooks and does domain writes such as branches,
pull requests, comments, labels, and merges. Its credentials live encrypted
in Postgres. They do not come from TOML, the environment, or a PEM file.

Until an operator connects GitHub, agent runs stop with a direct message.
`druks doctor` reports that no GitHub connection exists.

Connect it from **Settings → Services**. **Create GitHub App** starts the GitHub
manifest flow. Enter a GitHub organization, or leave the field empty for a
personal account. Accept the request on GitHub. Druks stores the credentials and
opens the installation page. Install the GitHub App on the applicable
repositories.

Before you create the app, set `urls.endpoint` to the dashboard base URL. If
you set `urls.webhook_host`, the webhook uses that host. Otherwise, it uses the
endpoint host.

You can paste the credentials of an existing GitHub App into the same card.
Enter the GitHub App ID, original PEM private key, and webhook secret. Druks
validates the credentials against GitHub and stores the app slug. Each operator
client then uses this service-identity row. Webhook deliveries use its stored
secret for validation.

To register the GitHub App manually, use this webhook URL:

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

Install the GitHub App on the repositories that Druks will use. This
installation set defines where `software_factory` can act. Personal access
tokens are not a supported substitute.

**To upgrade an existing installation**, paste the credentials one time on each
active host. Open Settings → Services. Connect GitHub with the existing operator
GitHub App ID, private key, and webhook secret. Do not create a replacement
GitHub App. The current webhook and installations continue to use the pasted
credentials.

### Review identity (optional)

The bundled `review` app can post its verdict reviews as a second
GitHub App, so GitHub accepts approvals on Druks-authored pull requests.
Configure it in **Settings → Review**. Enter the review GitHub App ID and its PEM
private key, both stored encrypted and empty-as-unset. Leave the pair empty and
reviews publish as operator comments. Set both values to publish separate
approval reviews. The review GitHub App needs read access to metadata and
contents, read/write access to pull requests, and no webhook.

`GITHUB_API_URL` defaults to `https://api.github.com` and can point every
client at another compatible GitHub API endpoint.

## Ticketing integrations

Tracker credentials are service identities. Connect Linear or Jira Cloud from
**Settings → Services**. The Linear identity uses an API key and webhook secret.
The Jira identity uses a base URL, email, API token, and webhook secret. Druks
validates the credentials before it stores them. Select the tracker and its
workflow statuses in **Settings → Software Factory**.

Webhook URLs remain `/_external/linear/events/` and
`/_external/jira/events/`. The Jira webhook uses a Jira Automation
**Send web request** action.

Select **Issue data (Jira format)** as its body.
Druks accepts the REST issue JSON under `issue`. Put the shared token in the
`x-druks-webhook-token` header. `druks doctor` treats a disconnected tracker as
optional. It reports pending setup if the selected tracker lacks a connection.

## Harnesses

Druks registers two subscription providers, `anthropic` and `openai`. Each
also accepts an API key. Both connect from **Settings → Providers**. The
connection flow stores each credential in Postgres. Druks refreshes a
subscription token on a schedule. It creates the CLI credential file inside
each sandbox, or passes the key in the CLI environment. It does not copy a
host login. This is a capability connection for the requesting account. In a
fresh `none`-mode install, the first completed subscription connection also
creates the operator account. See [access control](#public-urls-and-access-control).

**Add provider** searches Models.dev for providers that use one API key.
Druks caches the directory in Redis for one day for search and provider details.
**Save** stores the key and adds the provider and its model list.
When you remove the key, Druks removes the provider.

Provider details show documentation and API URLs from Models.dev.
Druks does not verify provider identity or restrict requests to the listed endpoint.
When an agent runs, OpenCode selects the endpoint.

Before you save a key, check the provider documentation and domain.

Cards show five-hour and general weekly limits with the time until reset.
Tooltips show exact reset times. **Catalog status** shows each provider's last
update. Anthropic and OpenAI fetch separate model lists. Added providers use
the cached Models.dev directory.

The `claude` and `codex` CLIs run on their own vendor's subscription or key.
`opencode` and `pi` run on an API key only. OpenCode can run a supported
Models.dev provider after its key is stored. A model ID is `provider/model`
for each harness, for example `openai/gpt-5.5`.

Process settings such as `DRUKS_CLAUDE_CONFIG_DIR` and
`DRUKS_CODEX_CONFIG_DIR` point at optional non-auth CLI configuration to carry
into sandboxes. The Compose deployment mounts these read-only. The default
harness, model, billing, effort, and timeout live in **Settings → Agents**;
each agent can override any of them on its app's page. **Unattended runs
(webhooks, schedules) run as** names the account whose subscription an
unattended run bills. A call refuses before provisioning a VM if the
credential it bills is missing.

## Sandboxes

| TOML key | Purpose |
| --- | --- |
| `sandbox.service_url` | Drukbox API base URL. An empty value disables sandbox-backed execution |
| `sandbox.service_token` | Drukbox API token |
| `sandbox.timeout` | Control-plane request timeout. The default is 180 seconds |
| `sandbox.image` | Optional provider image override |
| `sandbox.browser_login_proxy` | Login-window egress proxy. An empty value keeps the box IP |
| `sandbox.browser_login_tz` | Login-window timezone (IANA zone). An empty value keeps the container default |

`DRUKS_SANDBOX_KEYS_DIR` remains a process environment override for the
per-host SSH private-key directory.

`[sandbox].browser_login_proxy` sends the browser **login window** through an
HTTP proxy. The login then leaves from a different IP than the box. Use it for
sign-in flows that refuse a login from the box IP. Only the login window uses the
proxy. Borrowed sessions keep the box IP. This is sufficient after Druks makes
the session.

If you do not set the proxy, the login uses the box IP. If you set the proxy and
the exit is not available, the login browser fails. It does not fall back to the
box IP.

The value can include a user name and password (`http://user:pass@host:port`).
The login browser authenticates the proxy. You do not need an external relay.

Druks does not run the exit. You supply the exit and set this value to it. There
are two common types.

**Your own connection.** Do these steps:

1. Install Tailscale on a home device.
2. Make the device an exit node in the Tailscale app.
3. Add a `tailscale/tailscale` container to the deployment in userspace mode.
4. Set `TS_USERSPACE=true`.
5. Set `TS_OUTBOUND_HTTP_PROXY_LISTEN=:8080`.
6. Set `TS_EXTRA_ARGS=--exit-node=<your-device>`.
7. Set `browser_login_proxy = http://172.17.0.1:8080`.

The login then leaves from your home connection. The box keeps its own IP for all
other traffic. This exit needs no user name or password.

**A rented static-residential (ISP) proxy.** First make sure that a detection
service does not already know the IP as a proxy. Then set the proxy with its user
name and password: `browser_login_proxy = http://user:pass@isp-host:port`.
An ISP IP passes the datacenter-ASN check. A detection service can still find it
and mark it as a proxy.

`[sandbox].browser_login_tz` sets the timezone of the login browser. Use an IANA
zone name, for example `Europe/Madrid`. The browser reports a region, and the IP
has a region.

Set both to the same region. Some sign-in flows compare these values. If the
two regions are different, a flow can refuse the login. Only the login window
uses this value. If you do not set it, the browser keeps the container default
timezone.

`[sandbox].provider` accepts any Drukbox provider name. `docker` selects the
local install shape, `exe` selects the exe.dev + tailnet shape, and every other
name selects the generic remote shape. Provider-specific credentials and host
options live in `[sandbox.<provider>]`, and Drukbox interprets them. See
[deployment](deployment.md) or [full local setup](full-local.md) for the
topology.

## Notifications

Manage destinations from the dashboard. The current destination type is a Slack
incoming webhook. Actionable messages use Slack Block Kit. Other messages use
the same URL through Apprise.
`SLACK_SIGNING_SECRET` authenticates Slack interactivity callbacks.

Choose one enabled destination as the gate-notification destination in
Settings. A parked subjected run then produces a durable notification. Failure
to deliver the notification does not unpark or fail the run.

## MCP servers

`DRUKS_MCP_CATALOG` points to a JSON catalog of server definitions. Druks loads
this catalog at startup. The packaged catalog contains an empty `mcpServers`
map. Thus, a new installation has no built-in servers. A deployment can point
`DRUKS_MCP_CATALOG` to a mounted file with its defaults.

Druks always loads a
catalog. A missing catalog stops startup. Catalogs contain definitions, not
tokens.

`DRUKS_MCP_TRUSTED` points to the trust-pins JSON for the official registry
badge. Druks calculates the badge. An entry is official if its reversed
publisher namespace matches the remote host. For example, `com.grafana` matches
`*.grafana.com`. A pin covers a value that this rule cannot derive. The value
shape selects one of two pin types:

- A publisher namespace (`"grafana": "io.github.grafana"`) identifies a
  publisher that the rule cannot match. The entry URL stays live from the
  registry.
- An `http…` URL (`"sentry": "https://mcp.sentry.dev/mcp"`) supplies a hosted
  endpoint that the registry entry omits.

If the registry entry declares the hosted URL, pin the publisher. If it does
not declare the URL, pin the URL.

The dashboard can enable catalog entries and add custom servers. Authentication
is one of:

- A static token that Druks stores encrypted in Postgres
- A token from a named process environment variable
- An OAuth connection, which requires `urls.endpoint`.

Druks delivers enabled servers through the selected harness unless an app
workspace owns a required server with the same name. Tokens enter the agent
environment under a derived variable and are never returned by the API.

## Skills

The dashboard installs skill collections from GitHub repositories.
`DRUKS_SKILLS_DIR` selects the shared writable directory. Its default is
`<DRUKS_DATA_DIR>/skills`. A call receives the enabled skills that it requests.
If it requests none, it receives each enabled skill. A Software Factory build
requests the recommended set from the repository profile.

Druks excludes other
installed skills from the upload. The capability manifest records the delivered
set for each agent.

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

The first key encrypts new values. Each listed key can decrypt values. To rotate
the key, put a new key first in `druks.toml`. Then run the installer again:

```toml
[secrets]
secrets_key = "<new>,<old>"
```

While a stored row depends on the old key, keep that key. If you lose each key for a
row, you cannot recover that secret. Reconnect the OAuth grants. Enter the
static tokens again. Log in to the affected browser sessions again. Validation
and API errors do not include submitted secret values.

The encryption envelope does **not** currently cover harness subscription
payloads or notification webhook URLs. Postgres stores them as
ordinary Postgres fields, although APIs withhold or mask their values. Treat
access to Postgres and its backups as access to those credentials. GitHub App
private keys — the operator identity's and the review app's — are
database values under the envelope, no longer files mounted into the process.

## Registry access and sandbox templates

A Druks installation serves one operator or organization. Registry access and
template publishing are separate settings in `druks.toml`:

```toml
[registry]
host = "ghcr.io"
username = "builder"
password = "<registry-token>"

[templates]
repository = "acme/sandbox-templates"
```

For Docker Hub, set `registry.host = "docker.io"`. The host must have no URL
scheme or path. The template repository is a path within that registry,
with no tag or digest.

`druks setup` renders `REGISTRY_HOST`, `REGISTRY_USERNAME`, `REGISTRY_PASSWORD`,
and `TEMPLATE_REPOSITORY` for Drukbox. Do not duplicate these keys in
`[sandbox.<provider>]` or `[env]`. These settings require a Drukbox release
with separate registry access and template destination configuration.
When adopting that release, replace `EXE_IMAGE_REGISTRY`,
`EXE_REGISTRY_USERNAME`, and `EXE_REGISTRY_PASSWORD` in `[sandbox.exe]` with
the tables above. Split the full repository into its host and repository path.

Set all three registry values together. Registry access works without a
template destination: exe can boot other private images on that registry.
The registry's permissions determine which repositories the credential can
access. To build and publish templates, also set `templates.repository` and
use a credential with push permission. Leave the template repository blank
for local Docker builds without publication.

Druks sends labels such as `site-builder-build`. Drukbox creates tags such as
`ghcr.io/acme/sandbox-templates:site-builder-build-<build-id>` and pins the
published image by digest. App authors do not manage registry paths or
credentials. Druks writes configuration files with mode `0600`.

All template images share repository access and retention policy. Drukbox
does not delete remote registry manifests. Retain images that active templates use.

After editing `druks.toml`, render `.env` with the installation path and the
deployment user's home directory:

```bash
druks setup /path/to/install/.env --home /home/operator
```

Then recreate the Drukbox service with a release that supports these settings.
Use the installation's Compose files. A restart alone does not load a changed
container environment. Druks writes both configuration files with mode `0600`.

If a template already failed, use the authenticated Drukbox `GET /templates`
API to find the failed record. Delete only that record with
`DELETE /templates/{template_id}`, then run `druks doctor` in the Druks
container to create it again. Creating the same template without deleting
the failed record returns the existing failure.
