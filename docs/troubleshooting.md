---
title: "Troubleshooting"
description: "Diagnose startup, access, webhook, harness, sandbox, gate, recovery, and app-loading failures."
icon: "life-buoy"
---

Start with the non-mutating diagnostics:

```bash
cd ~/druks
docker compose ps
docker compose exec web druks doctor
docker compose logs --tail=200 web
```

`druks doctor` examines the full platform. It covers settings, secrets, service
credentials, data-directory writes, Postgres, Redis, Drukbox, harnesses, app
imports, capability modules, and app-owned checks. A failed check exits with a
nonzero status.

If the normal Drukbox check passes but real execution fails, use the opt-in
sandbox check:

```bash
docker compose exec web druks doctor --sandbox
```

It provisions, connects to, reattaches to, and releases a real host. It consumes
provider capacity and can take about a VM minute.

## The stack does not start

### `load_settings` fails

Read the named field in the error. Common causes:

- **Secrets key:** `secrets.secrets_key` is empty, has invalid base64, or does not decode to 32 bytes.
- A `druks.toml` value creates an invalid process setting.

Re-running `install.sh` renders `.env` from `druks.toml` and prints remaining
prerequisites. It does not replace a nonblank invalid value. Fix that value in
`~/druks/druks.toml`. Then run the installer again.

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

The web service never migrates on boot. Apply both the Druks and the Drukbox
migrations before starting the new images:

```bash
docker compose run --rm web druks init-db
docker compose run --rm drukbox .venv/bin/alembic upgrade head
docker compose up -d
```

The installer runs the same commands in this order.

## The dashboard is inaccessible

The edge authenticates the request. Druks maps its asserted identity to an account.
Distinguish the failure by what you see:

- If the browser redirects to `__exe.dev/login`, sign in at the edge.
- If the browser shows "couldn't resolve your identity," examine the configured
  identity header. Make sure that the proxy sends exactly one nonblank value.
- If the browser shows "Assertion rejected: …," use the error class to select
  the configuration. For signature or key errors, examine `identity.jwks_url`.
  For issuer or audience errors, examine the related claim setting. For expiry
  errors, compare the clocks of the edge and host.
- If onboarding asks you to connect a provider, complete that connection.
- If `none` mode returns 503, remove extra accounts or switch to `header` mode.
- If a run refuses to start, connect its selected harness.

Redis loss does not sign browsers out — there are no sessions. It only drops
in-flight connect attempts and other transient coordination.

Examine the identity configuration:

```bash
sed -n '/^\[identity\]/,/^\[/p' ~/druks/druks.toml | grep '^mode ='
grep -E '^(DRUKS_AUTH_HEADER|DRUKS_UPSTREAM)=' ~/druks/.env
docker compose logs --tail=200 caddy web
```

The local `docker` shape does not use Caddy. Use
[http://127.0.0.1:8001](http://127.0.0.1:8001). It runs with
`[identity].mode = "none"` — no authentication —
and must remain loopback-only.

## Webhooks are not arriving

Examine the webhook path:

1. Run `druks doctor`.
2. If `webhook_ingress` fails, fix DNS, TLS, or edge routing before you examine the provider.
3. Make sure that the provider URL is
   `https://<host>/_external/<provider>/events/`.
4. Make sure that its webhook secret matches the related `druks.toml` value.
5. Examine the provider delivery log and `docker compose logs web`.

When you set `urls.webhook_host`, the doctor sends an unsigned GitHub probe and
expects HTTP 401 from Druks. A different response means the request did not
arrive at the webhook verifier.

Druks deduplicates webhook delivery in Redis. A handler failure releases the claim
and returns an error so the provider can redeliver. App subscribers must
be idempotent.

## An agent cannot connect to `/mcp`

Examine the MCP path:

1. If the endpoint returns 401, mint a new token in **Settings → Tokens**.
2. Send the token as `Authorization: Bearer <token>`.
3. If the edge redirects or returns 404, run the installer again.
4. As an alternative, copy `deploy/caddy/Caddyfile`.
5. Then run `docker compose up -d caddy`.
6. Make sure that the backend answers directly:
   `curl -X POST http://127.0.0.1:8001/mcp` returns 401, never 404.

## An agent run will not start

### Harness not connected or expired

Open **Settings → Providers** and reconnect the Anthropic or OpenAI
subscription, or add the API key, that the model's provider needs. Host CLI
logins do not count. Druks validates the database credential before
provisioning a sandbox.

### Model has no harness

A model id is `provider/model`, such as `anthropic/claude-opus-4-7`; a bare id
names no harness. Clear a stale per-agent override in Settings or pick a model
from the picker.
Druks does not silently route an unknown model to another CLI.

### Drukbox is unreachable

Run:

```bash
docker compose exec web druks doctor
docker compose logs --tail=200 drukbox
```

Make sure that the service listens at `[sandbox].service_url` in `druks.toml`.
For remote providers, a healthy Drukbox API does not prove SSH access. Then
follow with `druks doctor --sandbox`.

### A sandbox process appears stuck

Druks copies the dashboard transcript from files that a detached VM process writes.
Druks polls over SSH and retries transient reconnects for up to five
minutes. Examine the agent call transcript and stderr first. Then examine the web and
Drukbox logs. A worker restart does not guarantee attachment to the same live
agent process. Recovery follows the durable operation boundary.

## A run waits

`parked` means that DBOS suspended the workflow on a gate. The workflow did not stall.
Open the subject detail page to see its current ask. In-app review offers
approve, request changes, or cancel. The owner system answers an external gate.

If no notification arrived:

- Make sure that an enabled destination is the gate destination.
- Examine the Notifications page and its recorded delivery error.
- If it is an in-app gate, answer from the subject page.

Notification failure deliberately leaves the run parked and resumable.

A gate times out after 14 days and the run fails with a gate-timeout failure
code. Cancelling a parked run clears the ask and frees its subject slot.

## A run is `failed`, `cancelled`, or `orphaned`

Use the state to select the next action:

- If the state is `failed`, examine the error text, last agent call, transcript, and stderr.
- If the state is `cancelled`, identify the operator or app reaction that stopped it.
- If the state is `orphaned`, do not attempt a resume. Its DBOS workflow row is absent.

Do not edit `durable_runs.state`. DBOS determines it, and the application cannot write it.
Do not mark an orphaned run as scheduled. Preserve the database. Determine why
the DBOS system state disappeared or why Druks uses the wrong database.

## A run repeats or does not reflect new settings

Completed durable operations reuse their recorded result. Settings read inside
a step remain the values that the run captured. A later settings edit affects a
new operation or run, not a completed checkpoint.

If an interrupted step repeats a side effect, that step lacks adequate
idempotency. The recovery guarantee is “reuse completed operations,” not “an
arbitrary line of Python executes exactly once.”

If two starts for the same subject return the same run ID, deduplication
operates correctly. Druks permits one active run for each workflow kind and
subject. Cancel
or finish the active run before expecting a new id.

## An app does not load

Run `druks doctor` and inspect the boot error. Typical causes:

- The entry-point key does not equal `App.name`.
- Duplicate installed distributions register the same name.
- The entry point does not resolve to an `App` subclass.
- An app table does not have its `<name>_` prefix.
- A capability is in `workflow.py` or `webhook.py` instead of a discoverable
  `workflows.py` or `webhooks.py` leaf.
- The app import raised an error.

The loader fails loudly because the app name owns API, settings, and
migration namespaces. See [writing an app](writing-an-app.md).

## Create a useful incident report

Record:

- The Druks image tag and `git` revision if you use a checkout
- The sandbox provider name and installation shape (`exe`, `docker`, or generic remote)
- The affected workflow ID, subject, state, and last agent-call ID
- The `druks doctor` output
- The applicable web and Drukbox logs without secret values
- The error point: before a completed step, during a gate, or after a deployment.

Do not include `druks.toml`, `.env`, PEM files, OAuth grants, sandbox credential
files, or raw MCP tokens.
