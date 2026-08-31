---
title: "Druks"
description: "The self-hosted runtime for durable agent apps."
sidebarTitle: "Overview"
---

Druks is a self-hosted runtime for durable agent apps. These apps can cross
process boundaries, wait for people, and retain an inspectable history after a
restart.

Your app owns the domain: its workflows, agents, prompts, models, routes, and
policy. Druks owns the operating substrate around it: durable execution,
Postgres-backed state, queues, gates, sandboxes, harnesses, events, settings,
and the shared dashboard.

## Why use Druks

An ordinary agent script assumes that its process stays alive. Real work breaks
this assumption. Models take time. Sandboxes disappear. Providers retry
webhooks. Deployments restart workers.

A human decision can arrive hours later.

Druks records completed durable operations. When execution recovers, the
workflow starts its orchestration again. It reuses the recorded results at the
same operation boundaries. Work that stops inside an operation can run again.
Thus, external writes still require idempotency.

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

Druks fits apps whose work spans durable operations, external triggers,
isolated agent calls, or waits. Examples include software delivery, incident
investigation, research review, approval flows, and periodic operational checks.

The bundled **Software Factory** app coordinates coding agents from a work item
to a reviewed pull request. It demonstrates the framework. GitHub policy and
software-delivery behavior belong to the app, not to Druks.

## Choose a path

- **Evaluate Druks:** Complete the [quickstart](quickstart.md) on one machine.
- **Understand recovery:** Read [concepts and guarantees](concepts.md).
- **Build an app:** Start with [writing an app](writing-an-app.md).
- **Give it screens:** Read the [Druks UI contract](druks-ui.md).
- **Run a production stack:** Follow the [deployment runbook](deployment.md).
- **Diagnose a failure:** Use [troubleshooting](troubleshooting.md).

## What Druks is not

Druks is not a model SDK or a sandbox provider. It does not preserve a live
agent process through a crash. It does not resume at an arbitrary Python line.
It does not guarantee exactly-once external side effects.

Drukbox provisions
hosts. Model providers and harness subscriptions remain separate. Your app owns
its domain policy.

Druks is under active development. Breaking changes can occur before version
1.0. `main` and `latest` are edge builds, not stable releases.
