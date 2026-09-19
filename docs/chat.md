---
title: "Chat"
description: "Talk to an agent, queue messages, and inspect its Druks tool calls."
icon: "messages-square"
---

Chat lets you talk to an agent that acts on Druks as your account.
A conversation is a live agent session, not a workflow. Chat has no Druks run,
gate, or park. Chat does not add entries to Activity.

Only the creator can read a conversation or receive its live events. On app
numbers with open access, operators do not see the chats. The people who hold
the number's phone read them there.
Chat uses Claude in this release. Chat declares a Bot with its own prompt and
settings row. Open **Chat → Channels → Agents** to change its harness, model,
billing, and effort. Unset fields inherit the
[installation settings](configuration.md#personal-and-installation-settings).
Chat's prompt and settings apply to web conversations and Chat's WhatsApp
numbers. The own-number fallback also uses Chat. Another app with paired access
uses that app's Bot prompt and settings. Operator turns have no timeout.
A prompt change reaches new conversations first. The agent keeps a session's
prompt until compaction.

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
An operator's Chat key permits every tool of the Druks toolkit: the routes tagged
`agent`. Builds keep a separate key with their restricted tool list. A key with a
tool list lists and calls only those tools. App numbers with open access use
such keys: see [WhatsApp](#whatsapp).

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

## WhatsApp

WhatsApp is a source for Chat. A person writes to a linked number, and Druks
saves the message in that person's conversation. The agent answers through its
MCP tools, and Druks sends the reply back to WhatsApp. A workflow never sees a
message. Druks reaches WhatsApp through [WAHA](https://github.com/devlikeapro/waha),
an open source WhatsApp HTTP API. Connect WAHA first: see
[WhatsApp](configuration.md#whatsapp).

### Link a number

A linked number is a connection. Its owner account holds the WAHA session, the
session's key, and the webhook secret. **Chat → Channels** offers two options.

**The assistant's own number is recommended.** Your private account stays
outside WAHA. Replies arrive as normal incoming messages and can ring under
your phone's notification settings. This needs one more WhatsApp number: a
prepaid SIM, or a virtual number that WhatsApp accepts.

1. Open **Chat → Channels**.
2. Under **Assistant's number**, select **Add number**.
3. Scan the QR code with the assistant's phone.
4. Select **Connect my phone**.
5. Send the exact code from your own WhatsApp to the assistant's number within
   10 minutes. Keep the code private.

The code connects the sender to your signed-in Druks account. Druks consumes
the code once and sends no proof message to the agent. Several operators can
share the assistant's number. Each operator uses their own code, and each
conversation belongs to that operator's account. Each operator gets the whole
Druks toolkit. The agent greets you after the phone connects. The agent receives
an internal message for this greeting, never the code.

Select **Disconnect my phone** to remove every phone paired to your account on
that number. Druks then ignores messages from those phones. Other operators
keep their pairings, and saved conversations stay available.

Druks ignores senders without a pairing before it saves a conversation or file,
or starts a turn. It also ignores messages from the assistant's own phone.
There is no admin, take-over, pause, or admin notice on that number. A bot
account owns the connection, but it owns none of the operator conversations.

**Your own number is the fallback.** Under **Your own number** on the same
Channels tab, select **Link your number**. This needs no second number. Only
your chat with yourself reaches your agent. Replies have no incoming-message
sound. WAHA receives your direct chats, but Druks ignores other senders.
Druks adds `[Druks] ` to replies in the self-chat. Saved message text stays plain.

**Other app numbers** appear in that app's **Settings → Channels** when it
declares a [Bot](writing-an-app.md#answer-whatsapp-with-a-bot). Select **Add number**
there. A Bot with open access gets a bot account and a bot admin account.
These accounts never sign in.

Druks creates the WAHA session and its key, saves the connection, and then
writes the session's config. Scan the QR code from **Linked devices** in
WhatsApp on the phone. The number is live when WAHA reports that the session
works. When WhatsApp unlinks the device, the number shows **Disconnected** and
its QR code again. Druks refuses a number that another live connection holds. **Remove**
deletes the WAHA session and its key, and ends the pauses of the number's chats.
The connection and its chats stay as history, and each new link is a new
connection.

### Who writes

On other app numbers with open access, each person has one conversation.
Druks selects the account, prompt, and tools by sender:

| Sender | Owner | Prompt | Tools |
| --- | --- | --- | --- |
| The number's admin | The number's bot admin account | Druks's admin prompt | The Bot's `admin_tools`, `answer_gate`, `chat_resume_conversation` |
| Anyone else | The number's bot account | The Bot's `prompt` | The Bot's `user_tools` |

The agents of a number have no shell, file, or web tools. Their harness, model,
billing, effort, and timeout come from the Bot's row in the app's
**Settings → Agents**. A turn that runs past the timeout stops, and Druks marks
it `interrupted`. A bot or bot admin account holds no credential of its own, so its
agents and runs bill the default account.

All pending messages of a WhatsApp conversation go into one turn. Druks saves
media as a Druks file on its message. Druks drops a repeated message by its
WhatsApp ID. Druks writes to a person only to answer them, one reply for each
turn. It never starts a chat and never sends in bulk. Before it sends a reply,
Druks takes a new message ID from WAHA and records it. WAHA's copy of the sent
message then carries a known ID, even when the copy arrives before the send
returns.

Druks also adds **internal messages** to a conversation. Each one comes from a
fixed template. An internal message starts a turn like any message, and it
never goes to WhatsApp. Druks talks to agents, and agents talk to people.

### Admins

Each app number with open access has an admin from the moment it links: the person who holds its
phone. They talk to the admin's agent in the phone's chat with itself, **Message
yourself** in WhatsApp. This needs no setup and no second phone. WhatsApp does
not ring for a message that an account sends to itself, so the phone shows
Druks's questions in that chat without a sound. Druks adds `[Druks] ` to each
reply in the self-chat, including admin questions. Saved message text stays
plain. Replies to another person have no prefix.

To get the questions on another phone, which rings, select **Add admin** on the
number. This opens a one-time code that expires after 10 minutes. Druks then
sends the number's questions to the person who sends exactly that code from
their own WhatsApp, and the admin's agent greets them. A number has one such
person. Both chats belong to the number's bot admin account, which never signs
in. The only text that Druks reads in a message is an open admin code.

### Confirmation

A run records the conversation whose tool call started it. On app numbers
with open access, when such a run
parks with an in-app question, Druks adds an internal message to the admin's
conversation. The message holds the request, the person it is about, the run ID,
and when the run started to wait. The admin's agent asks the admin, and answers
with `answer_gate`. When two questions are open, the run ID tells them apart.

Only the number's admin can answer a question that came from the number's
chats with open access. The dashboard, `answer_gate`, and notification buttons all refuse
everyone else, operators included.

An operator conversation has no admin restriction, including one on the
assistant's number. The operator answers through the normal gate controls.
Druks posts no admin question for that conversation.

When a run that waited ends, Druks adds an internal message with its result or
its failure to the conversation that started it. The Bot then tells the person.
A cancelled run adds nothing.

### Taking over

On app numbers with open access, a message from the number's phone pauses that chat. Druks
saves the typed text as an internal message. Messages that arrive during a pause
start no turn: they go into the chat's next turn. Druks tells the admin about the
pause. The pause ends when the phone stays quiet in that chat for 4 hours, and
each typed message starts the 4 hours again. To end it sooner, the admin tells
their agent, which calls `chat_resume_conversation`. The pause is a DBOS
workflow that the typed message names, and a chat has at most one open pause.
