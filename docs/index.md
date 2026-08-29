---
title: "Druks"
description: "The self-hosted runtime for durable agent apps."
sidebarTitle: "Overview"
---

Druks is the self-hosted runtime for agent apps that must survive restarts,
cross process boundaries, wait for people, and leave an inspectable record of
what happened.

Your app owns the domain: its workflows, agents, prompts, models, routes, and
policy. Druks owns the operating substrate around it: durable execution,
Postgres-backed state, queues, gates, sandboxes, harnesses, events, settings,
and the shared dashboard.

## Why use Druks

An ordinary agent script assumes its process stays alive. Real work does not:
models take time, sandboxes disappear, providers retry webhooks, deployments
restart workers, and a human decision can arrive hours later.

Druks records completed durable operations. When execution recovers, the
workflow runs its orchestration again and reuses those recorded results at the
same operation boundaries. Work interrupted inside an operation may run again,
so external writes still require idempotency.

That gives an app a practical control loop:

```text
event or schedule
      ↓
durable workflow
      ↓
agent calls in isolated sandboxes
      ↓
typed result or human gate
      ↓
recovery, history, and operations in one place
```

## What you can build

Druks fits apps whose work spans several durable operations, external triggers,
isolated agent calls, or waits for people and systems. Examples include software
delivery, incident investigation, research review, approval flows, and recurring
operational checks.

The bundled **Software Factory** app coordinates coding agents from a work item
to a reviewed pull request. It demonstrates the framework; GitHub policy and
software-delivery behavior belong to that app, not to Druks itself.

## Choose a path

- **Evaluate Druks:** complete the [quickstart](quickstart.md) on one machine.
- **Understand recovery:** read [concepts and guarantees](concepts.md).
- **Build an app:** start with [writing an app](writing-an-app.md).
- **Run a production stack:** follow the [deployment runbook](deployment.md).
- **Diagnose a failure:** use [troubleshooting](troubleshooting.md).

## What Druks is not

Druks is not a model SDK or a sandbox provider. It does not preserve a live
agent process through a crash, resume at an arbitrary Python line, or guarantee
exactly-once external side effects. Drukbox provisions hosts; model providers
and harness subscriptions remain separate; your app still owns domain policy.

Druks is under active development. Before 1.0, expect breaking changes and use
`main` and `latest` as edge builds rather than stable releases.
