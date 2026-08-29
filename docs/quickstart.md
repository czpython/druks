---
title: "Quickstart"
description: "Install Druks locally, connect an agent harness, and verify the complete execution path."
icon: "rocket"
---

This path runs Druks, Postgres, Redis, and Drukbox on one machine. It is the
fastest way to evaluate the real system: the same durable engine and isolated
sandbox path used by a hosted installation, without public ingress.

## Prerequisites

- Docker with the Compose plugin
- enough Docker capacity for the stack and short-lived sandbox containers
- a Claude or Codex subscription to run an agent

## Install

Run the installer with its default `docker` sandbox provider:

```bash
curl -fsSL https://druks.ai/install.sh | bash
```

The installer creates `~/druks`, generates the local secrets, applies database
migrations, pulls the images, and starts the stack. It does not read or reuse
Claude or Codex credentials from your host.

Verify the services and API:

```bash
cd ~/druks
docker compose ps
curl -fsS http://127.0.0.1:8001/health
```

The health response should be:

```json
{"status":"ok"}
```

Open [http://127.0.0.1:8001](http://127.0.0.1:8001) to reach the dashboard.

## Connect a harness

Open **Settings → Harnesses** and connect Claude or Codex. The provider verifies
the subscription and Druks stores the resulting connection in Postgres. A fresh
local installation finishes onboarding when the first harness is connected.

Run the platform preflight:

```bash
docker compose exec web druks doctor
```

Then prove the control plane can create and remove a real sandbox:

```bash
docker compose exec web druks doctor --sandbox
```

The second command does real provisioning. It is stronger evidence than a
healthy Drukbox HTTP endpoint.

## Run an app

Druks does not create a generic job by itself; an installed app supplies the
workflow and its trigger. The distribution includes **Software Factory** as its
reference app.

To use it:

1. Open **Settings → Services** and create or connect the operator GitHub App.
2. Install that GitHub App on the repository you want Druks to work in.
3. Open **Software Factory → Projects**, create a project, and add the repository.
4. Profile the repository, then use the configured ticket or GitHub trigger to
   start work.
5. Watch the work item, event feed, agent calls, and any parked gate in the
   dashboard.

A loopback installation cannot receive GitHub, Linear, or Jira webhooks from the
internet. Use an HTTPS tunnel for provider-driven triggers, or continue locally
with dashboard-initiated actions. The exact paths are listed in
[full local setup](full-local.md#webhook-caveat).

## Next steps

- Read [concepts and guarantees](concepts.md) before judging recovery behavior.
- Follow [full local setup](full-local.md) for browser sessions, webhook ingress,
  and sandbox-image changes.
- Build a separately packaged app with [writing an app](writing-an-app.md).
- Use [deployment](deployment.md) when the dashboard needs a durable public home.
