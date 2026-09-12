---
title: "Chat"
description: "Start operator conversations, set autonomy, and reuse a warm sandbox across turns."
icon: "message-square"
---

Chat is a bundled app. It ships with Druks. It is not an optional package.

Open **Chat** in the dashboard rail. Each conversation belongs to the signed-in
operator. Other accounts cannot read it.

## Start a conversation

1. Open **Chat → Conversations**.
2. Choose **New**.
3. Enter the first message. A title is optional. An empty title takes the
   first line of that message.
4. Choose **Start**.

That start creates the thread and one Talk run. Later lines answer the parked
turn. **Send** appends your line and continues. **Stop** ends Talk and reaps
the sandbox. Stop does not write a message.

## Autonomy

Each conversation has a mode. A change applies on the next agent call.

| Mode | Tools |
| --- | --- |
| **propose** (default) | Read tools only. A write is refused, and the agent sees no mutating tool. The agent describes the action in the thread. You do it in the dashboard. |
| **confirm** | The live catalog is visible. A write through an MCP tool is recorded, not performed. After the turn, a gate asks you to approve or reject. The thread then says what ran. |
| **full** | Writes run at once as your account. |

Set the mode on the conversation. The next Talk call reads the live row. The
mode holds at the door, so it covers every route and not only the MCP tools.
Only a write that comes through an MCP tool can be recorded for confirm; a
write by any other route is refused.

Chat uses the same `/mcp` catalog as an external agent. It is not a second
catalog and it does not replace [Connect your agent](connect-your-agent.md).
The sandbox talks inward to this appliance as you, through a call-scoped token
that dies with the agent call.

## Idle window

Talk parks each turn with `hold_sandbox` of 15 minutes. The park itself still
lasts days. The hold only clips the Drukbox lease so the warm VM stays up for
a short idle window.

Send inside that window and Talk reuses the same host. It does not provision
a new one. After the clipped lease ends, the next line provisions a cold host
and still works.

**Stop** reaps the host. A worker crash still frees the VM when the Drukbox
lease lapses. `review()` and any park without `hold_sandbox` still delete the
host at once.

Set `urls.endpoint` so a sandbox can reach this appliance. Without it a turn
fails and names the setting. A Docker sandbox rewrites a loopback dashboard URL
to `host.docker.internal`. See
[public URLs](configuration.md#public-urls-and-access-control).
