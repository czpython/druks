# Troubleshooting

Start with the non-mutating diagnostics:

```bash
cd ~/druks
docker compose ps
docker compose exec web druks doctor
docker compose logs --tail=200 web
```

`druks doctor` checks settings, secrets, GitHub App credentials and
installations, optional ticketing integrations, data-directory writes,
Postgres, Redis, Drukbox, harness connections, extension imports, capability
module names, and extension-owned checks. A failed check exits nonzero.

Use the opt-in sandbox check only when the normal Drukbox check passes but real
execution fails:

```bash
docker compose exec web druks doctor --sandbox
```

It provisions, connects to, reattaches to, and releases a real host. It consumes
provider capacity and can take about a VM minute.

## The stack does not start

### `load_settings` fails

Read the named field in the error. Common causes:

- `DRUKS_SECRETS_KEY` is empty, not valid base64, or does not decode to 32 bytes
- a mounted PEM path differs from the path visible inside the container
- a hand-edited `.env` contains a malformed value

Re-running `install.sh` preserves existing values, fills generated values, and
prints remaining prerequisites. It does not overwrite a nonblank broken value;
fix that value in `~/druks/.env`.

### Postgres or Redis is unreachable

```bash
docker compose ps postgres redis
docker compose logs --tail=200 postgres redis
docker compose exec web druks doctor
```

Druks and DBOS use Postgres for durable state. Do not delete the Postgres volume
as a recovery step. Redis holds transient coordination, but losing it can drop
in-flight webhook claims, OAuth state/cache entries, and provisioning-gate
state.

### A migration is missing

The web service never migrates on boot. Apply both Druks and, on a remote
profile, Drukbox migrations before starting the new images:

```bash
docker compose run --rm web druks init-db
docker compose run --rm sandbox-service .venv/bin/alembic upgrade head
docker compose up -d
```

The installer performs these steps in this order.

## The dashboard is inaccessible

The shipped remote edge admits any request whose configured
`DRUKS_AUTH_HEADER` carries a nonempty trusted identity; from there the app
itself asks you to connect a harness, which mints the session cookie. A
redirect to `__exe.dev/login` means no trusted identity header reached Caddy.
A dashboard that loads but immediately shows the connect screen means the
session cookie is missing or expired — sign in with Codex or Claude again
(Redis loss signs everyone out but never touches stored credentials).

Check:

```bash
grep -E '^(DRUKS_AUTH_HEADER|DRUKS_UPSTREAM)=' ~/druks/.env
docker compose logs --tail=200 caddy web
```

The local `docker` profile intentionally skips Caddy; use
<http://127.0.0.1:8001>. It has no application login and should remain
loopback-only.

## Webhooks are not arriving

1. Run `druks doctor`. If `webhook_ingress` fails, fix DNS, TLS, or edge routing
   before debugging the provider.
2. Confirm the provider URL is
   `https://<host>/_external/<provider>/events/`.
3. Confirm its webhook secret matches the corresponding environment value.
4. Inspect the provider's delivery log and `docker compose logs web`.

When `DRUKS_WEBHOOK_HOST` is set, the doctor sends an unsigned GitHub probe and
expects HTTP 401 from Druks. A different response means the request did not
reach the webhook verifier.

Webhook delivery is deduplicated in Redis. A handler failure releases the claim
and returns an error so the provider can redeliver. Extension subscribers must
be idempotent.

## An agent cannot reach `/mcp`

1. A 401 means the personal access token is missing, malformed, expired, or
   revoked — mint a fresh one in **Settings → Agent access** and resend it as
   `Authorization: Bearer <token>` ([Connect your agent](connect-your-agent.md)).
2. A redirect or 404 at the edge means the host's Caddyfile predates the
   `/mcp` handlers: deploys never refresh the host copy, so re-run the
   installer (or copy `deploy/caddy/Caddyfile`) and `docker compose up -d
   caddy`.
3. Confirm the backend answers directly:
   `curl -X POST http://127.0.0.1:8001/mcp` returns 401, never 404.

## An agent run will not start

### Harness not connected or expired

Open **Settings → Harnesses** and reconnect the selected Claude or Codex
harness. Host CLI logins do not count. Druks checks the database credential
before provisioning a sandbox.

### Model has no harness

Models are restricted to the model lists registered by the shipped harnesses.
Clear a stale per-agent override in Settings or select a current listed model.
Druks does not silently route an unknown model to another CLI.

### Drukbox is unreachable

Run:

```bash
docker compose exec web druks doctor
docker compose logs --tail=200 sandbox-service sandbox-janitor
```

For the local provider, Drukbox runs on the host, not in this Compose project.
Confirm it listens at the URL in `DRUKS_SANDBOX_SERVICE_URL`. For remote
providers, a healthy Drukbox API does not prove SSH reachability; follow with
`druks doctor --sandbox`.

### A sandbox process appears stuck

The dashboard transcript is copied from files written by a detached process in
the VM. Druks polls over SSH and retries transient reconnects for up to five
minutes. Check the agent call's transcript and stderr first, then the web and
Drukbox logs. A worker restart does not guarantee attachment to the same live
agent process; recovery follows the durable operation boundary.

## A run is waiting

`pending_input` means the DBOS workflow is suspended on a gate, not stalled.
Open the subject detail page to see its current ask. In-app review offers
approve, request changes, or cancel; an external gate is answered by its owning
system.

If no notification arrived:

- confirm an enabled destination is selected as the gate destination
- inspect the Notifications page and its recorded delivery failure
- answer from the subject page if it is an in-app gate

Notification failure deliberately leaves the run parked and resumable.

A gate times out after 14 days and the run fails with a gate-timeout failure
code. Cancelling a parked run clears the ask and frees its subject slot.

## A run is `failed`, `cancelled`, or `orphaned`

- `failed`: the workflow raised; inspect its failure text, last agent call, and
  transcript/stderr.
- `cancelled`: an operator or application reaction asked DBOS to stop it.
- `orphaned`: the Druks run row exists, but its DBOS workflow row has been
  missing for more than five minutes. It cannot resume.

Do not edit `durable_runs.state`; it is derived from DBOS and is not a writable
column. Do not mark an orphaned run as scheduled. Preserve the database and
determine why DBOS system state was removed or the wrong database is connected.

## A run repeats or does not reflect new settings

Completed durable operations reuse their recorded result. Settings read inside
a step intentionally remain the values captured by that run; a later settings
edit affects a new operation or run, not a completed checkpoint.

If an interrupted step repeats a side effect, that step lacks adequate
idempotency. The recovery guarantee is “reuse completed operations,” not “an
arbitrary line of Python executes exactly once.”

If two starts for the same subject return the same run id, deduplication is
working: only one active run per workflow kind and subject is allowed. Cancel
or finish the active run before expecting a new id.

## An extension does not load

Run `druks doctor` and inspect the boot error. Typical causes:

- entry-point key does not equal `Extension.name`
- duplicate installed distributions register the same name
- the entry point does not resolve to an `Extension` subclass
- an extension table lacks its `<name>_` prefix
- a capability lives in `workflow.py` or `webhook.py` instead of a discoverable
  `workflows.py` or `webhooks.py` leaf
- the extension's import raised

The loader fails loudly because the extension name owns API, settings, and
migration namespaces. See [writing an extension](writing-an-extension.md).

## Collecting a useful incident report

Record:

- Druks image tag and `git` revision if running from a checkout
- provider profile (`exe`, `aws`, or `docker`)
- affected workflow id, subject, state, and last agent-call id
- `druks doctor` output
- relevant web and Drukbox logs with secret values removed
- whether the failure happened before a step completed, during a gate, or after
  a deploy

Do not include `.env`, PEM files, OAuth grants, sandbox credential files, or
raw MCP tokens.
