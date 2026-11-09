---
title: "Druks"
description: "AI agents work. You make the call."
sidebarTitle: "Overview"
---

Druks finds the work, does it on the tools you already use, and brings the
important decisions to you.

Druks is a self-hosted or cloud platform where AI agents such as Claude Code
and Codex do a company's recurring engineering work, and a person approves each
change.

- **It finds the work.** A schedule, an alert, a ticket or a button starts it.
  Nobody writes a prompt.
- **It does the work with your tools.** Claude Code or Codex, with your own
  Claude or OpenAI account, in a sandbox, on your real repositories. You see
  the cost after every step.
- **You make the call.** The run stops and asks you before it changes anything:
  Approve, Change or Stop. It waits up to 14 days, and no agent runs while it
  waits.

## What a run looks like

The included **Software Factory** app takes a ticket to a pull request:

1. A ticket or a GitHub event starts the run.
2. A coding agent writes the change in a sandbox and checks it.
3. When the app needs a decision, the run stops and asks you. You see what it
   wants to do, why, and what it cost so far.
4. It opens a pull request for review.

Each question, answer and cost stays in the run history.

## Two ways to start

Druks Cloud and self-hosted Druks work the same way. You get the same apps and
you approve in the same places. The only difference is where Druks runs.

<CardGroup cols={2}>
  <Card title="Druks Cloud" icon="cloud" href="/cloud">
    Druks runs on our servers. Sign in and connect your repositories. Nothing
    to install.
  </Card>
  <Card title="Self-hosted" icon="terminal" href="/quickstart">
    Druks runs in Docker on your laptop or your own server. Your AI installs it
    for you.
  </Card>
</CardGroup>

## Where to go next

| I want to… | Start here |
| --- | --- |
| See my first approval | [Quickstart](quickstart.md) |
| Know how approvals work and where I answer them | [Approvals](approvals.md) |
| Run Druks for my team | [Deployment](deployment.md) and [configuration](configuration.md) |
| Automate my own process | [Writing an app](writing-an-app.md) |
| Understand restarts and what is saved | [Concepts and guarantees](concepts.md) |
| Fix a problem | [Troubleshooting](troubleshooting.md) |

## How Druks works

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
- **Talk to an agent:** Open [Chat](chat.md).
- **Run a production stack:** Follow the [deployment runbook](deployment.md).
- **Diagnose a failure:** Use [troubleshooting](troubleshooting.md).
