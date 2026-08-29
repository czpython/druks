---
title: "Quickstart"
description: "Install Druks locally, connect an agent harness, and examine the complete execution path."
icon: "rocket"
---

This path runs Druks, Postgres, Redis, and Drukbox on one machine. It uses the
same durable engine and isolated sandbox path as a hosted installation. It does
not require public ingress.

## Prerequisites

- Docker with the Compose plugin
- Sufficient Docker capacity for the stack and short-lived sandbox containers
- A Claude or Codex subscription to run an agent.

## Install

Run the installer with its default `docker` sandbox provider:

```bash
curl -fsSL https://druks.ai/install.sh | bash
```

The installer creates `~/druks` and generates the local secrets. Then it applies
database migrations, pulls the images, and starts the stack. It does not read
or reuse Claude or Codex credentials from your host.

Make sure that the services and API operate:

```bash
cd ~/druks
docker compose ps
curl -fsS http://127.0.0.1:8001/health
```

The health response is:

```json
{"status":"ok"}
```

Open the dashboard at [http://127.0.0.1:8001](http://127.0.0.1:8001).

## Connect a harness

Open **Settings → Harnesses** and connect Claude or Codex. The provider validates
the subscription. Druks stores the connection in Postgres. The first harness
connection completes the setup of a new local installation.

Run the platform preflight:

```bash
docker compose exec web druks doctor
```

Then make sure that the control plane can create and remove a real sandbox:

```bash
docker compose exec web druks doctor --sandbox
```

The second command does real provisioning. It is stronger evidence than a
healthy Drukbox HTTP endpoint.

## Run an app

Druks does not create a generic job. An installed app supplies the workflow and
its trigger. The distribution includes **Software Factory** as its reference
app.

To use it:

1. Open **Settings → Services**.
2. Create or connect the operator GitHub App.
3. Install that GitHub App on the repository that Druks will use.
4. Open **Software Factory → Projects**.
5. Create a project and add the repository.
6. Profile the repository. Then use the configured ticket or GitHub trigger to
   start work.
7. Watch the work item, event feed, agent calls, and each parked gate in the
   dashboard.

A loopback installation cannot receive GitHub, Linear, or Jira webhooks from the
internet. Use an HTTPS tunnel for provider triggers. You can also use local
dashboard actions. The exact paths are in
[full local setup](full-local.md#webhook-caveat).

## Next steps

- Read [concepts and guarantees](concepts.md) before judging recovery behavior.
- Follow [full local setup](full-local.md) for browser sessions, webhook ingress,
  and sandbox-image changes.
- Build a separately packaged app with [writing an app](writing-an-app.md).
- If the dashboard requires a durable public home, use [deployment](deployment.md).
