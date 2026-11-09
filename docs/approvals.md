---
title: "Approvals"
description: "How a run waits for your yes, and where you give it."
icon: "circle-check"
---

An app decides where a person must make the call. At that point the run stops
and asks you. Nothing changes until you answer.

## What you see

- **The question,** in one sentence. For example: "I'd like to upgrade 3
  packages in acme/api."
- **Why,** with the facts: what it fixes and whether the tests pass.
- **The plan,** when you want the details: the diff or the list of changes.
- **The cost so far.**
- **Your choices.** Usually Approve, Change or Stop. Some apps ask a short
  question instead.

## While the run waits

- **No agent runs.** Druks releases the sandboxes of the run while it waits.
- **It survives restarts and deploys.** After your answer, the run continues
  from the same point.
- **It waits up to 14 days.** After 14 days without an answer, the run fails
  with a gate-timeout failure. See [troubleshooting](troubleshooting.md).

## Where you answer

You answer in the same places on Druks Cloud and on a self-hosted Druks.

| Where | How |
| --- | --- |
| Dashboard | Open the run. The waiting step shows the question and the buttons. |
| Slack | Druks posts the question with buttons to the Slack channel you select. See [notifications](configuration.md#notifications). |
| Chat | Ask what waits for you, and answer in the [chat](chat.md). |
| Claude Code or Codex | Connect the Druks MCP server. Your agent lists what waits, reads the question and answers it. See [connect your agent](connect-your-agent.md). |

Druks accepts one answer for each question, from whichever place answers
first.

## After you answer

- **Approve:** the run continues and makes the change, for example it opens a
  pull request.
- **Change:** you write what to change. The agent tries again and asks you
  again.
- **Stop:** the run ends. Nothing changes.

Each question, answer and cost stays in the run history, with who answered and
when.

## For app builders

In code, an approval is a gate. See
[wait for input](writing-an-app.md#wait-for-input) in the app guide, and
[waiting for people and systems](concepts.md#waiting-for-people-and-systems).
