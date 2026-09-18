---
title: "Chat"
description: "Talk to an agent, queue messages, and inspect its Druks tool calls."
icon: "messages-square"
---

Chat lets you talk to an agent that acts on Druks as your account.
A conversation is a live agent session, not a workflow. Chat has no DBOS
workflow, gate, or park. Chat does not add entries to Activity.

Only the creator can read a conversation or receive its live events.
Chat uses Claude in this release. It runs on the installation's execution
defaults: harness, model, billing, effort, and fast mode, as set in
[installation settings](configuration.md#personal-and-installation-settings).
Chat has no model selector of its own.

## Start a conversation

1. Open **Chat** in the dashboard sidebar.
2. Select **New conversation**.
3. Write your message.
4. Select **Send**, or press Enter.

The first message creates the conversation. It shows as **New conversation**
until the agent's first reply. Then Claude names it in a short separate call.
New lines use Shift + Enter.

**Connecting…** appears at the next reply position until the agent can reply.
The first message can take longer than later messages. The composer stays
available while you wait.

## Send, stop, and send again

You can send a message while the agent replies. Druks saves the message and
shows **Queued** until its turn. A conversation processes one turn at a time.
A turn consists of one user message and its reply.

**Stop** appears beside **Send** during Connecting and while a turn runs.
It cancels only that message, not the conversation. Other queued messages
go next. Sandbox startup continues. The next message reuses the sandbox, or
the sandbox expires with its lease.

A cancelled turn shows **Cancelled**. Druks keeps the text that the agent
already sent. **Send again** saves a new message with the same text.

If a turn dies, Druks shows **Interrupted**. Druks never sends that turn again
by itself. **Send again** saves a new message with the same text. This action
can cause the agent to do an external action again.

A browser connection loss does not send the message again. The page connects
again and reads the saved messages and available live events.

## Read replies and tool calls

Agent text streams into the conversation. A tool row shows its ACP title
and status. Select the row to read its input and output.
Tool rows have no timestamps. Saved replies retain their tool details and
their position between text segments.

An agent plan shows the latest complete plan. A plan update replaces the
previous list. Tool updates instead change only the fields that the adapter
sends. Completed replies retain text and tool calls, not the live plan.

The page stays at the bottom while text arrives. Scroll up to read earlier
messages. **Latest message** returns to the bottom.

## Agent access

The agent uses the installation's `/mcp` endpoint as the conversation's owner.
Its Chat key permits every Druks MCP tool. Builds keep a separate key with
their restricted tool list.

The agent can change Druks through those tools. Chat has no permission dialog
or proposal mode. The adapter runs in bypass mode and cannot call
`AskUserQuestion`.

The sandbox receives credential placeholders. The Drukbox proxy exchanges
them for real credentials. See [public URLs and access control](configuration.md#public-urls-and-access-control)
for the MCP address and edge requirements.

## Execution and recovery

Each account has one leased sandbox that serves all its conversations. When
the Chat credentials change, the next turn replaces the sandbox, and a turn
still running in the old sandbox ends as `interrupted`. A detached
bridge runs in the sandbox and starts one ACP adapter per conversation. Druks
opens an SSH channel only when it communicates with the bridge.

Postgres stores conversations and messages. A reply links to the message it
answers and keeps the tool calls it made. Your messages have five durable
states: `pending`, `delivered`, `replied`, `interrupted`, and `cancelled`. A
`delivered` message is the one the agent is answering. Redis stores the
delivery locks, the read positions, and the live events. The bridge determines
whether a turn runs.

Delivery and Stop each use a conditional Postgres update from `pending`.
Delivery changes the state to `delivered` just before the prompt request. Stop
changes the state to `cancelled`. The first update wins. Delivery never sends
a message that Stop cancelled while pending.

After delivery wins, Stop sends ACP `session/cancel` through the bridge.
The bridge matches the message ID, so a delayed Stop cannot cancel the next
turn. Druks saves the partial reply and marks the message `cancelled`.
Delivery remains at most once. An uncertain delivery can leave a message
interrupted. Druks does not send it again automatically.

The bridge numbers the current turn's events and writes them to disk. Druks
reads the events after its last position and streams them only to the owner's
open pages.
After a completed turn, Druks saves the adapter's session files as a Druks
file. The next saved archive replaces the previous archive.

### Druks restarts

The agent keeps its process in the sandbox. Each delivery is a DBOS workflow.
When Druks starts again, DBOS runs an interrupted delivery again. It follows
the turn from the last read position and saves the reply.

### The bridge or sandbox dies mid-turn

The next contact finds that the turn is gone and marks it `interrupted`.
Druks does not resend it. The latest saved session archive contains only
the session files from the last completed turn.

### The sandbox is idle

Nothing renews an idle sandbox. It expires with its lease. The next message
gets a new sandbox and reloads the saved session files. Each turn renews the
sandbox lease and its identity expiry. The lease is 150 minutes.
