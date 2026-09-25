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
| Deployment | installation timezone, identity, ingress, Drukbox, encryption key | `~/druks/druks.toml` |
| Dashboard | personal timezone, the GitHub connection, harness and tracker credentials, workflow and agent overrides, MCP servers, skills | Postgres |

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
| `[sandbox]` | Drukbox provider, service URL and token, image override, and the proxy and issuer addresses |
| `[sandbox.<provider>]` | Provider environment passed through to the remote stack |
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

## Personal and installation settings

One Druks installation serves one organization. Separate organizations use
separate installations. **Settings → Agents** owns the shared harness, model,
billing, effort, fast mode, and timeout defaults. Every account uses these
defaults. Shared agent overrides take priority. A declared agent timeout also
takes priority over the installation default.

**Settings → Preferences** edits the timezone on your `Account`. The personal
settings API also edits your account's gate notification destination. Druks
copies the installation timezone and notification default when it creates an
account. Later installation changes do not change existing account preferences.
Each page saves its own draft.

`PATCH /api/settings` accepts `gateParkDestinationId` as the notification default
for **new accounts only**. Set it to a destination ID, or to `null` to start new
accounts with gate notifications off. This default has no dashboard control.
Use `PATCH /api/settings/personal` to change an existing account's destination.
Clearing or replacing the installation default does not change existing accounts.

Set the installation timezone at the top level of `druks.toml`, before any table:

```toml
timezone = "Europe/Madrid"
```

Use an IANA timezone. The default is `UTC`. Restart Druks after a change to
apply it to all schedules.

The first account becomes the default account, including in header and JWT modes.
Unattended calls use its subscriptions. The flag grants no extra
permissions. Calls with an explicit account use that account's subscriptions.
Missing subscriptions fail the call. API keys belong to the installation.

The installation timezone controls schedules and operational day boundaries.
Your personal timezone controls timestamp display. Gate notifications use the
run account's preferences. Unattended runs record the default account.
Druks refuses to start a run before account setup.

**Schedules**, below **Usage**, lists workflows that declare a schedule. You can
change cadence, pause, or resume each schedule. Preset and pause changes save
automatically. Custom cron input saves when you press Enter or leave the field.
**Use defaults** removes both overrides and restores the declared cadence and
enabled state. The same fields remain in app settings.

The page shows the installation timezone. Account timezone preferences do not
change schedule timing. A failed save keeps your edits. A read refresh also
preserves unsaved edits. If you leave the page with unsaved edits, Druks asks
first. The `?app=` filter selects one installed app.

**Next run** estimates the next invocation from the saved cadence. It does not
confirm scheduler health. **Last run** and **Last runs** show recorded schedule
invocations from DBOS, including dispatch ticks. They do not show the downstream
work that dispatch starts. History contains up to eight invocations per schedule.

**Run now** queues one invocation through DBOS without a change to the cadence or
pause state. A paused schedule stays paused. The invocation uses the same entry
and default account as its scheduled ticks.

The API exposes installation settings at `GET/PATCH /api/settings` and your
preferences at `GET/PATCH /api/settings/personal`. The personal route uses the
authenticated account. It returns `timezone` and `gateParkDestinationId`. It
rejects execution settings. The installation API rejects timezone changes.

## Core process settings

| Variable | Default | Purpose |
| --- | --- | --- |
| `DRUKS_DATABASE_URL` | local `druks` Postgres | Runtime and DBOS database |
| `DRUKS_TEST_DATABASE_URL` | local `druks_test` Postgres | What the shipped pytest fixtures use — never the runtime's |
| `DRUKS_TEST_REDIS_URL` | `redis://127.0.0.1:6379/15` | What the shipped pytest fixtures flush |
| `DRUKS_REDIS_URL` | `redis://127.0.0.1:6379/0` | Short-lived coordination and caches |
| `DRUKS_DATA_DIR` | `/var/lib/druks` | Logs, artifacts, installed skills |
| `DRUKS_HARNESS_CONFIG_ROOT` | `~/.config/druks/harnesses` | Optional harness configuration copied into sandboxes |
| `DRUKS_LOG_LEVEL` | `INFO` | Python and DBOS log level |

Postgres stores durable state. Redis does not store workflow state. It supports
short-lived concerns including webhook delivery claims, OAuth state and token
caches, and the sandbox provisioning gate.

## Public URLs and access control

| TOML key | Purpose |
| --- | --- |
| `urls.endpoint` | Browser-visible dashboard URL and MCP OAuth callback base. It is also the `/mcp` base when `urls.webhook_host` is empty |
| `urls.webhook_host` | Public webhook hostname and the HTTPS host for this installation's `/mcp` endpoint |
| `identity.mode` | `none` (default, no authentication, single operator), `header` (edge-asserted identity), or `jwt` (validated edge-signed assertion) |
| `identity.header` | The trusted identity header. The shipped Caddy edge also uses it. Header and JWT modes have no default and require it |
| `identity.jwks_url` | `jwt` mode: where the edge publishes its signing keys |
| `identity.jwt_issuer` | `jwt` mode: required `iss` claim value |
| `identity.jwt_audience` | `jwt` mode: required `aud` claim value |
| `identity.jwt_identity_claim` | `jwt` mode: the JSON Pointer to the account identity in the verified payload (default `/email`) |

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
   Druks resolves `identity.jwt_identity_claim` in the verified payload and maps
   the selected value to an account. A validation error
   returns a 401 without token material. Druks uses a fixed RS256
   configuration and does not negotiate it.
4. **No-authentication mode (`none`).** This mode has no authentication or identity edge. Druks
   resolves the only account. Zero accounts is the setup state. The
   first completed provider connection creates the operator account from the
   provider-validated email.

   More than one account is configuration
   drift. Druks refuses requests and startup in this state.

In JWT mode, `identity.jwt_identity_claim` uses
[JSON Pointer (RFC 6901)](https://www.rfc-editor.org/rfc/rfc6901.html).
Set it in the deployment's `druks.toml`:

```toml
[identity]
jwt_identity_claim = "/traits/email"
```

`/email` selects a top-level claim. `/traits/email` selects a nested claim.
`/people/0/email` selects a claim from the first object in an array. Dots are
literal key characters. Within a key, encode `/` as `~1` and `~` as `~0`.
For example, `/https:~1~1id.example~1email` selects the key
`https://id.example/email`. Druks refuses invalid pointer syntax at startup
in JWT mode. Bare claim names and dot paths are not accepted.

The selected value must be a nonblank string. Druks removes its outer
whitespace. A missing path, invalid traversal, or another result shape
returns a 401 without creating an account. The extraction has no
provider-specific behavior.

A subscription is always one person's. An API key is the installation's:
one per provider, owned by no account, and visible to every account in
**Settings → Providers** with the name of the person who last pasted it. A
paste from any account replaces it. Disconnect clears the secret and retains
the credential identity for historical agent-call billing references.

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
provider-authenticated `/_external/*` and PAT-authenticated `/mcp` routes. The
issuer listener serves only the `/api/secrets/*` route, which authenticates the
sandbox's identity bearer. These routes do not resolve the header. A future
public listener must keep the same isolation.

Public `POST /_external/*` routes bypass the identity gate and use their own
authentication. Webhooks use signature validation. The notification response
route uses its correlation token. `GET /api/auth/me` answers without a
resolved account so the dashboard can render onboarding in the `none`-mode
setup state.

## Personal access tokens

Agents and other non-browser clients use personal access tokens for the
internal API. Mint these tokens in Settings → API tokens. Send a token as
`Authorization: Bearer <token>`. A token has the form
`druks_pat_<prefix>_<secret>`. Druks stores only the SHA-256 hash of the full
token. It shows the plaintext one time and expires the token after 365 days.

If the header is present, it must authenticate. A bad token returns a 401.
Druks does not use edge identity as a fallback. Token management accepts only a
signed-in identity. It refuses requests that contain an `Authorization` header.
Thus, a leaked token cannot mint or revoke tokens.

If someone compromises a token, mint a replacement. Then revoke the old token in
Settings → API tokens. Revocation is immediate. The list shows the prefix and last
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

Connect it from **Settings → Connections → Services**. **Create GitHub App**
starts the GitHub manifest flow. Enter a GitHub organization, or leave the field empty for a
personal account. Accept the request on GitHub. Druks stores the credentials and
opens the installation page. Install the GitHub App on the applicable
repositories.

Before you create the app, set `urls.endpoint` to the dashboard base URL. If
you set `urls.webhook_host`, the webhook uses that host. Otherwise, it uses the
endpoint host.

You can paste the credentials of an existing GitHub App into the same card.
Enter the GitHub App ID, client ID, client secret, original PEM private key,
and webhook secret. Druks validates the App ID and the key against GitHub and
stores the app slug. Each operator client then uses this service-identity row.
Webhook deliveries use its stored secret for validation. On a connected card, a
secret you leave blank stays as it is.

The App's slug is the handle people tag in an issue or pull request comment,
for example `@druks-acme`. A person signs in to Druks through the same App, and
their sign-in links their GitHub account to their Druks account: see
[Chat](chat.md#github). The client ID and secret are the App's OAuth client.
A new paste with the same client ID keeps every linked account. A paste with
another client ID revokes them.

To register the GitHub App manually, use this webhook URL:

Webhook URL:
`https://<webhook-host>/_external/github/events/`

Callback URL:
`<endpoint>/api/oauth/callback`

Subscribe to issue comment, pull request, pull request review, pull request
review comment, and push events. Keep **Expire user authorization tokens** on:
Druks refreshes a person's sign-in with the refresh token that comes with it,
and refuses a sign-in that has none.

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

A sandbox never holds an installation token. It holds a placeholder in
`GH_TOKEN`, and git and `gh` read it through Drukbox's setup. The Drukbox
secrets proxy swaps the placeholder for a token that Druks mints for the
sandbox's repo, with the expiry GitHub gives it, and fetches a new one before
it expires. The `software_factory` build clones and pushes as the operator App.
A review clones as the reviewer App when one is connected. Builds and reviews
also get the GitHub MCP server for `api.githubcopilot.com`. It acts as the
review identity through a second entry.

**To upgrade an existing installation**, paste the credentials one time on each
active host. Open **Settings → Connections → Services**. Connect GitHub with the
existing operator GitHub App ID, client ID, client secret, private key, and
webhook secret. Do not create a replacement GitHub App. The current webhook and
installations continue to use the pasted credentials. An App created before
Druks answered on GitHub has no client ID on its card. Until it has one,
**Connect GitHub** fails. Open the card and select **Replace connection**. Enter the client ID from the App's settings page and a client
secret you generate there. Leave the other fields blank, and save. Then add the
callback URL to the App on GitHub.

### Review identity (optional)

The bundled `software_factory` app declares an optional service, **Github
Reviewer**: a second GitHub App, so GitHub accepts approvals on Druks-authored
pull requests. Connect it in **Settings → Connections → Services** with the App
ID and its PEM private key, both stored encrypted. Leave it unconnected and
reviews publish as operator comments. Connect it and reviews publish as
approval reviews, and a review sandbox clones as it. The reviewer App needs
read access to metadata and contents, read/write access to pull requests, and
no webhook.

`GITHUB_API_URL` defaults to `https://api.github.com` and can point every
client at another compatible GitHub API endpoint.

## Ticketing integrations

Select the tracker in **Software Factory → Settings**. The default is Linear.
**none** leaves Software Factory without a ticket tracker.

**Linear** and **Jira** are service identities. Connect them from
**Settings → Connections → Services**. The Linear identity uses an API key
and webhook secret. The Jira identity uses a base URL, email, API token, and
webhook secret. Druks validates the credentials before it stores them. Linear
and Jira share five status settings: trigger, in progress, in review, done, and
resting. When the selected tracker is connected, the settings page lists its
statuses. After you change the tracker, save the settings to list the statuses of
the new tracker. An empty in review or resting status leaves the ticket where it is.

Select **druks** to use the ticket board on this appliance. That choice needs no
credentials. The status settings stay hidden. The trigger status is
Ready for Agent, and it is not a setting.

The dashboard shows the board and the ticket pages only for **druks**. Each
ticket selects a GitHub repository from a Software Factory project. Druks derives
each project's ticket prefix from the project name. It takes the first two
letters and one later letter, and it uses the first prefix that no other project
holds. An identifier is `{prefix}-{n}`. Druks mints it once, and a move to
another repository keeps it.

A ticket that enters Ready for Agent opens a build against the selected
repository. If a scheduled, running, or parked run already exists for that
ticket, Software Factory does not start another.

An agent reads and answers a board ticket through this appliance's own `/mcp`. A
build whose tracker is **druks** asks for that server. You paste nothing and
connect nothing. Druks mints the token of the account the run belongs to, and it
allows only the five ticket tools. A comment the agent writes then carries that
person's name. The token appears in that person's API tokens. Druks mints another
once they retire it. The server uses this installation's
[/mcp address](#public-urls-and-access-control). The box must reach that address.
The agent reads a ticket with
`software_factory_get_ticket` and posts with `software_factory_add_comment`.

Webhook URLs remain `/_external/linear/events/` and
`/_external/jira/events/`. The Jira webhook uses a Jira Automation
**Send web request** action.

Select **Issue data (Jira format)** as its body.
Druks accepts the REST issue JSON under `issue`. Put the shared token in the
`x-druks-webhook-token` header. `druks doctor` treats a disconnected Linear or
Jira identity as optional when that tracker is not selected. It reports pending
setup if the selected tracker is Linear or Jira and that identity is missing.

Software Factory starts a Linear or Jira build under the Druks account of the
ticket assignee. It finds that account from the assignee ID in the webhook. The
ID must match the [provider account](#oauth-grant-identity) of an MCP connection
that the assignee made with **Connect your account**. A connection for everyone
has no account, so it does not match. If no account or more than one account
matches, the build uses the default account. A **druks** ticket build uses the
account of the ticket assignee.

## WhatsApp

**Waha** is the service identity that Druks reaches WhatsApp through.
[WAHA](https://github.com/devlikeapro/waha) is an open source WhatsApp HTTP API
that you run beside Druks. Connect it
from **Settings → Connections → Services** with WAHA's address and a key. Druks
uses this key only to create and delete a linked number's session and its
session key. That is WAHA's admin key when you run WAHA yourself. Every other
call uses the session key of the number. Druks checks neither value when you
save the card, so a wrong key shows up when you link a number.

Run WAHA with the NOWEB or GOWS engine: Druks takes each reply's message ID from
WAHA before it sends the reply. Druks refuses a number that links on another
engine. WAHA must reach this webhook URL:

Webhook URL:
`https://<webhook-host>/_external/waha/events/`

Set `urls.webhook_host`, or `urls.endpoint`, before you link a number. Druks
writes the URL, the session's webhook secret, and the chats to ignore (status
updates, groups, channels, and broadcasts) into each session's config. Each
event carries an HMAC SHA-512 signature, and Druks refuses an event without a
valid one. See [Chat](chat.md#whatsapp) for linking numbers.

## Slack

**Slack** is the service identity that Druks answers Slack messages as: one Slack
app in one workspace. Create the app from Druks's manifest, install it, and paste
its keys.

1. Set `urls.endpoint`. Set `urls.webhook_host` when Slack must reach Druks at
   another host.
2. Open **Settings → Connections → Services → Slack** and select **Create Slack
   App**. Slack opens its app creation page with Druks's manifest filled in.
3. Select the workspace, review the app, and create it.
4. Install the app in the workspace. Slack shows the bot token on **OAuth &
   Permissions**, and the client ID, client secret, and signing secret on
   **Basic Information**.
5. Paste the four values on the Slack card. Druks checks the bot token with
   `auth.test` and keeps the workspace and the bot user.
6. In the Slack app's **Event Subscriptions**, verify the Request URL again.
   Slack's first check ran before the card held the signing secret, so Druks
   refused it.

The manifest asks for the bot scopes `chat:write`, `channels:history`,
`groups:history`, `im:history`, `mpim:history`, `users:read`, and `files:read`.
It subscribes the bot to direct messages and to the rooms it is invited to,
leaves token rotation off, and names these URLs:

Events URL:
`https://<webhook-host>/_external/slack/events/`

Redirect URL:
`<endpoint>/api/oauth/callback`

Druks checks each event's signature with the signing secret, and refuses an
event older than five minutes. With rotation off, the pasted bot token and each
person's Slack token live until someone revokes them. A person connects their
own Slack account through the same app: see [Chat](chat.md#slack).

## Harnesses

Druks registers two subscription providers, `anthropic` and `openai`. Each
also accepts an API key. Both connect from **Settings → Providers**. The
connection flow stores each credential in Postgres. Druks refreshes a
subscription token on a schedule. A sandbox holds a placeholder for the
subscription token and never the token. Drukbox fetches the token from the
Druks issuer through the sandbox's identity, and the secrets proxy swaps the
placeholder on each request to the entry's host. See
[sandbox identities and the issuer](concepts.md#agents-harnesses-workspaces-and-sandboxes).
Druks does not copy a host login. This is a capability connection for the
requesting account. In a fresh `none`-mode install, the first completed
subscription connection also creates the operator account. See
[access control](#public-urls-and-access-control).

A `claude` sandbox reads its placeholder from `ANTHROPIC_AUTH_TOKEN`. A
`codex` sandbox reads its placeholder from `CODEX_SUBSCRIPTION_TOKEN`. The
Codex run wrapper writes `~/.codex/auth.json` from that variable before the
command. The file carries the account id, the sentinel refresh token
`druks-placeholder`, and an unsigned id token with the account id, the plan,
and the email. Codex sends the placeholder to `chatgpt.com` on every request.
Codex never refreshes it: the one refresh it attempts after a 401 fails on
the sentinel, and the turn ends. The real refresh token and id token stay in
Postgres.

An API key never enters the sandbox. Druks gives the key to Drukbox as a
secret entry when it creates the sandbox. The sandbox holds a placeholder in
the variable the entry names, and the CLI reads it from the environment. The
Drukbox secrets proxy swaps the placeholder for the key in the entry's header
on each request to the entry's host.

| Harness | Credential | Variable | Host | Header |
| --- | --- | --- | --- | --- |
| `claude` | Anthropic subscription | `ANTHROPIC_AUTH_TOKEN` | `api.anthropic.com` | `Authorization: Bearer` |
| `codex` | OpenAI subscription | `CODEX_SUBSCRIPTION_TOKEN` | `chatgpt.com` | `Authorization: Bearer` |
| `claude`, `pi`, `opencode` | Anthropic API key | `ANTHROPIC_API_KEY` | `api.anthropic.com` | `x-api-key` |
| `pi`, `opencode` | OpenAI API key | `OPENAI_API_KEY` | `api.openai.com` | `Authorization: Bearer` |
| `codex` | OpenAI API key | `CODEX_API_KEY` | `api.openai.com` | `Authorization: Bearer` |

The `ANTHROPIC_AUTH_TOKEN` and `OPENAI_API_KEY` entries come from the Drukbox
catalog. Druks declares the other entries with their host and header.

The Compose stack runs the secrets proxy on every provider but docker-sbx. See
[the secrets exchange and the secrets proxy](deployment.md#the-secrets-exchange-and-the-secrets-proxy).
Without it, Drukbox refuses the sandbox and the call fails.

**Add provider** searches Models.dev for providers that use one API key.
Druks caches the directory in Redis for one day for search and provider details.
**Save** stores the key and adds the provider and its model list.
When you remove the key, Druks removes the provider.

Provider details show documentation and API URLs from Models.dev.
Druks does not verify provider identity or restrict requests to the listed endpoint.
When an agent runs, OpenCode selects the endpoint.

Before you save a key, check the provider documentation and domain.

Provider rows show access state and weekly quota when available. Open
**Manage** for credential controls, the last subscription token refresh, the
5-hour quota when available, and the model catalog timestamp. The weekly quota
stays in the provider row.
Anthropic and OpenAI fetch separate model lists.
Added providers use the cached Models.dev directory.

Druks polls subscription usage every five minutes, with intervals up to one hour
while values stay unchanged. An exhausted window waits for its reset unless an
agent call finishes on the subscription. Manual refresh keeps a 60-second
minimum between polls.

The `claude` and `codex` CLIs run on their own vendor's subscription or key.
`opencode` and `pi` run on an API key only, for Anthropic or OpenAI. A key for
a Models.dev provider stores, but an agent on that provider refuses to run: no
proven transport carries its placeholder through the secrets proxy. A model ID
is `provider/model` for each harness, for example `openai/gpt-5.5`.
The harness menus disable `opencode` and `pi` until a provider API key is configured.

`paths.harness_config_root` points at optional CLI configuration that Druks
carries into sandboxes. The installer creates the root. Compose mounts it
read-only at `/harnesses`. Claude and Codex each read their named directory:

```text
~/.config/druks/harnesses/
├── claude/
│   ├── .claude.json
│   ├── CLAUDE.md
│   ├── settings.json
│   └── plugins/
└── codex/
    ├── AGENTS.md
    └── config.toml
```

Missing files are optional. Druks copies no credentials file, and it removes
the `mcpServers` block from `.claude.json` before the copy. MCP credentials
are sandbox entries. Provider credentials do not belong in this root. OpenCode
and Pi do not read it.
The default harness, model, billing, effort, and timeout live in
**Settings → Agents**. Each agent can override any of them on its app's page.
**Unattended runs use** names the default account. Shared execution settings
select subscription or API key billing. Subscription billing uses the run
account's subscription. API key billing uses the installation key. A call refuses
before provisioning a VM if its selected credential is missing.

## Sandboxes

| TOML key | Purpose |
| --- | --- |
| `sandbox.service_url` | Drukbox API base URL. An empty value disables sandbox-backed execution |
| `sandbox.service_token` | Drukbox API token |
| `sandbox.timeout` | Control-plane request timeout. The default is 180 seconds |
| `sandbox.image` | Optional provider image override |
| `sandbox.proxy_url` | The secrets proxy, at the address a sandbox dials. The docker shape sets `http://172.17.0.1:8880`. docker-sbx leaves it empty |
| `sandbox.issuer_url` | The issuer base URL the secrets exchange dials. The default is `http://127.0.0.1:8001`. For a Drukbox on another server, set the address of the Druks host that Drukbox reaches. The installer then serves the issuer route there ([the issuer listener](deployment.md#the-issuer-listener)) |
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

The dashboard has no notifications page yet. Manage destinations through the
API. The current destination type is a Slack incoming webhook. Actionable
messages use Slack Block Kit. Other messages use the same URL through Apprise.
The [Slack card](#slack)'s signing secret authenticates the button clicks that
Slack sends back.

Select one enabled destination as the gate-notification destination through
the API. A parked subjected run then produces a durable notification. Failure
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

- Secret headers, which Druks keeps in the vault. A bearer token is the
  `Authorization` header spelled out; the form's Bearer field composes it.
- An OAuth connection, which requires `urls.endpoint`.

Druks gives OAuth discovery and client registration 30 seconds in total.
A timeout names the stage that was pending. Retry the connection.

Druks delivers enabled servers through the selected harness unless an app
workspace owns a required server with the same name. Each OAuth bearer and
each secret header is a Drukbox entry behind a vault row. The sandbox holds a
placeholder under a derived variable, and the harness configuration names that
variable. The secrets proxy swaps the placeholder only for the server's host.
The Druks issuer answers the value from the row that was bound when the
sandbox was created. A pasted token reaches a running sandbox within five
minutes. A server enabled after that gets no entry in a running sandbox. The
API never returns a token.

### OAuth grant identity

At connect and reconnect, Druks records the provider account behind an OAuth
grant. It tries these sources in order:

1. The ID token in the token response. Druks checks its issuer, audience,
   time claims, and nonce. The token comes directly from the token endpoint
   over TLS, so Druks does not check its signature.
2. The userinfo endpoint, if it is on the identity issuer's origin. Druks
   does not follow redirects.

Druks stores the issuer, the `sub` claim, the source, and the name and email
if they are present. It stores `email_verified` only if the provider sends a
Boolean. `identity_status` is `resolved`, `unavailable` if the provider has
no source, or `failed`. A failed lookup does not change the connection.
`identity_error` records the reason for a failed lookup, such as an HTTP
status or an invalid identity response. It contains no provider response
body or token. The log carries the underlying error. A reconnect records a
new outcome.

The identity comes from the authorization server itself if it offers `openid`.
It comes from the OpenID provider at the origin root if that provider has the
same authorization and token endpoints. If a provider offers `openid`, Druks
requests the MCP resource's scopes with `openid`, `email`, and `profile`, and
registers its client with the same scopes. An existing grant gets an identity
at its next reconnect.

Druks stores the `scope` field of the token response as the granted scopes.
If the field is missing, the provider granted the requested scopes. If the
field is missing and Druks requested no scopes, the value is `null`. These
facts do not change account ownership or login. Software Factory uses them to
select the account for a Linear or Jira build. See
[ticketing integrations](#ticketing-integrations).

## Skills

The dashboard installs skill collections from GitHub repositories. A private
repository needs the GitHub App installed on it.
`DRUKS_SKILLS_DIR` selects the shared writable directory. Its default is
`<DRUKS_DATA_DIR>/skills`. A call receives the enabled skills that it requests.
If it requests none, it receives each enabled skill. A Software Factory build
requests the recommended set from the repository profile.

Druks excludes other
installed skills from the upload. The capability manifest records the delivered
set for each agent.

## Credential custody and secrets at rest

Druks keeps every secret it holds in one table, the vault. A vault row has a
kind, an audience, and an encrypted mapping of secrets:

| Kind | Audience | What the row keeps |
| --- | --- | --- |
| `static` | `provider:<id>`, `mcp:<name>` | A pasted API key, or one secret header of an MCP server |
| `app_key` | `service:<slug>` | A GitHub App private key and webhook secret |
| `oauth` | `service:<slug>`, `mcp:<name>` | A refresh token and the client that refreshes it |
| `subscription` | `provider:<id>` | The token payload of a provider subscription |

A revoked row keeps its facts and loses its secrets. An agent call keeps its
reference to the row it billed. A reconnect revives the row.

`secrets.secrets_key` encrypts the vault and the browser-session payloads with
AES-256-GCM. Each database column supplies authenticated associated data, and
each value gets a derived encryption key. The setting is one or more
comma-separated, base64-encoded 32-byte master keys:

```bash
python3 -c 'import base64, os; print(base64.b64encode(os.urandom(32)).decode())'
```

The first key encrypts new values. Each listed key can decrypt values. To rotate
the key, put a new key first in `druks.toml`. Then run the installer again:

```toml
[secrets]
secrets_key = "<new>,<old>"
```

While a stored row depends on the old key, keep that key. If you lose each key
for a row, you cannot recover that secret. Reconnect the OAuth grants and the
subscriptions. Enter the static tokens again. Log in to the affected browser
sessions again. Validation and API errors do not include submitted secret
values.

`secrets.drukbox_secrets_key` encrypts the secret entries of each sandbox in
the Drukbox database. The installer generates it and renders it as
`SECRETS_KEY` for the Drukbox API and the secrets exchange. Rotate it as you
rotate `secrets_key`, with the new key first.

The envelope does **not** cover notification webhook URLs. Postgres stores
them as ordinary fields, although the API masks their values. Treat access to
Postgres and its backups as access to those values. GitHub App private keys,
the operator identity's and the review identity's, are vault rows, not files
mounted into the process.
