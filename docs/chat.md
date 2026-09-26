---
title: "Chat"
description: "Talk to an agent, queue messages, and inspect its Druks tool calls."
icon: "messages-square"
---

Chat lets you talk to an agent that acts on Druks as your account.
A conversation is a live agent session, not a workflow. Chat has no Druks run,
gate, or park. Chat does not add entries to Activity.

Only the creator can read a conversation or receive its live events. Operators
do not see the chats of an app's WhatsApp numbers: the people who hold the
number's phone read them there.
Chat runs on Claude, Codex, or OpenCode. Its harness, model, billing, and
effort come from Chat's row in **Chat → Channels → Bots**. A field that you
leave unset uses the
[installation settings](configuration.md#personal-and-installation-settings).
OpenCode ignores the effort: its effort values are model variants, and Druks's
levels are not among them.

## Start a conversation

1. Open **Chat** in the dashboard sidebar.
2. Select **New conversation**.
3. Write your message.
4. Select **Send**, or press Enter.

The first message creates the conversation. It shows as **New conversation**
until the agent's first reply. Then the harness names it in a short separate call.
New lines use Shift + Enter.

The starter prompts fill the message field. They do not send a message.
The new conversation page also lists the installed apps.

## Find and pin conversations

Search filters conversation titles. It ignores letter case and does not search
message text. Search does not close the current conversation or clear its draft.

Select the pin beside a conversation or in its header to keep it in **Pinned**.
Pins are saved for your account and stay after a page reload. Select the pin
again to remove it. A pin does not change a conversation's message times.

The remaining conversations appear under **Today**, **Yesterday**, or **Earlier**,
using the timezone in your preferences. Each group shows the most recent message
first. The header shows the last saved agent reply time. Times include a date
for messages from previous days.

On a narrow screen, Chat shows either the list or the conversation. Use
**Back to conversations** to return to the list. Your draft stays in the message
field when you switch conversations.

## Wait for a reply

Replies use the **Agent** label. Messages that Druks writes for the agent use
**Druks**. Your messages use **You**.

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
tool list lists and calls only those tools. The agents of a WhatsApp number have
such keys: see [WhatsApp](#whatsapp).

The agent can change Druks through those tools. Chat has no permission dialog
or proposal mode. Claude runs in bypass mode and cannot call `AskUserQuestion`.
Codex runs in full-access mode for an operator. For a Bot it runs in read-only
mode without shell, web, or image tools. OpenCode runs its default agent with
every permission allowed for an operator. For a Bot it runs an agent that
denies the file, shell, and web tools.

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
file. The next saved archive replaces the previous archive. A conversation that
moves to another harness starts a fresh agent session and keeps its messages.

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
session's key, and the webhook secret. There are three kinds:

- **An app's number.** In the app's settings, open **Channels** and select
  **Add number**. The tab shows only when the app declares a
  [Bot](writing-an-app.md#answer-whatsapp-with-a-bot). Druks creates a bot
  account and a bot admin account for the number. These accounts never sign in.
- **The assistant's number, recommended.** Open **Chat → Channels** and select
  **Add number**. Scan the QR code with a phone that holds a second WhatsApp
  number. Then select **Connect my phone** and send the code from your own
  WhatsApp to that number. You now talk to your agent there, under your account
  and with the whole Druks toolkit. Replies arrive as normal incoming messages,
  and WAHA never sees your private chats. Several operators can share the
  number: each one connects their own phone. Druks ignores every sender that
  has no connected phone. **Disconnect my phone** removes your phones from the
  number. This number has no admin and no take-over.
- **Your own number, the fallback.** On the same tab, select **Link your
  number**. This needs no second number. Only your chat with yourself reaches
  your agent, and Druks ignores everyone else on that number. WhatsApp gives no
  sound for a message that an account sends to itself, and WAHA receives your
  private chats.

Druks creates the WAHA session and its key, saves the connection, and then
writes the session's config. Scan the QR code from **Linked devices** in
WhatsApp on the phone, which shows the device as Druks. The number is live when
WAHA reports that the session works. When WhatsApp unlinks the device, the
number shows **Disconnected**, and **Link again** takes a new scan on the same
connection, which keeps its chats, its admin, and its connected phones. Druks
refuses a number that another live connection holds. **Remove** deletes the WAHA
session and its key, and ends the pauses of the number's chats.
The connection and its chats stay as history, and each new link is a new
connection.

### Who writes

Each person who writes to a number has one conversation. Druks sends each
message to a conversation by its sender:

| Sender | Owner | Prompt | Tools |
| --- | --- | --- | --- |
| The number's admin | The number's bot admin account | Druks's admin prompt | The Bot's `admin_tools`, `answer_gate`, `chat_resume_conversation` |
| Anyone else | The number's bot account | The Bot's `prompt` | The Bot's `user_tools` |

The agents of a number have no shell, file, or web tools. Their harness, model,
billing, effort, and timeout come from the Bot's row in the app's
**Settings → Bots**. A turn that runs past the timeout stops, and Druks marks
it `interrupted`. A bot or bot admin account holds no credential of its own, so its
agents and runs bill the default account.

All pending messages of a WhatsApp conversation go into one turn. Druks saves
media as a Druks file on its message. An image reaches the agent with its
message. Any other file except audio reaches it as a link to a copy in the
sandbox. An operator's agent opens the copy with its tools. A number's bot
agents have no file tools, so they see the link and the file's name. Druks
drops a repeated message by its WhatsApp ID. Druks writes to a person only to
answer them, one reply for each turn. It never starts a chat and never sends in
bulk. Before it sends a reply, Druks takes a new message ID from WAHA and
records it. WAHA's copy of the sent message then carries a known ID, even when
the copy arrives before the send returns.

A voice note becomes text before its turn. Druks sends the audio to the
[Speech To Text card](configuration.md#speech-to-text) and saves the words on
the message. The agent reads them under what the person typed, marked as a
voice note, and answers in text. Nothing goes to the person before the reply.
When Druks cannot transcribe a note, when no card is connected, or when the
note is over 25 MiB, the agent gets an internal message instead. It then tells
the person in one line to write instead. The web page shows the typed text, the
words, and a link to the audio.

Druks also adds **internal messages** to a conversation. Each one comes from a
fixed template. An internal message starts a turn like any message, and it
never goes to WhatsApp. Druks talks to agents, and agents talk to people.

### Admins

Each app number has an admin from the moment it links: the person who holds its
phone. They talk to the admin's agent in the phone's chat with itself, **Message
yourself** in WhatsApp. This needs no setup and no second phone. WhatsApp does
not ring for a message that an account sends to itself, so the phone shows
Druks's questions in that chat without a sound. Druks puts `[Druks] ` before
each reply that it sends into a number's chat with itself, so you can tell the
agent's replies from your own messages.

To get the questions on another phone, which rings, select **Add admin** on the
number. This opens a one-time code that expires after 10 minutes. Druks then
sends the number's questions to the person who sends exactly that code from
their own WhatsApp, and the admin's agent greets them. A number has one such
person. Both chats belong to the number's bot admin account, which never signs
in. The only text that Druks reads in a message is an open admin code.

### Confirmation

A run records the conversation whose tool call started it. When such a run
parks with an in-app question, Druks adds an internal message to the admin's
conversation. The message holds the request, the person it is about, the run ID,
and when the run started to wait. The admin's agent asks the admin, and answers
with `answer_gate`. When two questions are open, the run ID tells them apart.

Only the number's admin can answer a question that came from the number's
chats. The dashboard, `answer_gate`, and notification buttons all refuse
everyone else, operators included.

When a run that waited ends, Druks adds an internal message with its result to
the conversation that started it. When a run fails, waited or not, Druks adds an
internal message with its failure. The Bot then tells the person: a failure in
one short line, with no error details and no retry. A cancelled run adds
nothing.

### Taking over

A message that someone types on the number's phone pauses that chat. Druks
saves the typed text as an internal message. Messages that arrive during a pause
start no turn: they go into the chat's next turn. Druks tells the admin about the
pause. The pause ends when the phone stays quiet in that chat for 4 hours, and
each typed message starts the 4 hours again. To end it sooner, the admin tells
their agent, which calls `chat_resume_conversation`. The pause is a DBOS
workflow that the typed message names, and a chat has at most one open pause.

## Slack

Slack is a source for Chat. You write to the Druks bot in a direct message, or
tag it in a room, and your own agent answers there, under your account and with
your whole toolkit.
One Slack app serves every app in Druks. Connect the Slack card first: see
[Slack](configuration.md#slack). **Chat → Channels** shows the workspace, the
bot, and your Slack account while the card is connected.

### Link your account

Druks knows you by your Slack account. Select **Connect Slack** on the Slack
pane. Slack asks you to sign in, and Druks saves your Slack token under your
account, like a Gmail connection. Other apps can use that grant. **Disconnect**
revokes it.

When you write to the bot before you connect, the bot answers with a private
link. Open it, sign in to Druks, and connect Slack. Druks then answers the
message you wrote. The link works for 10 minutes. A forwarded link is harmless:
Slack's sign-in links whoever opens it, and the held message waits for the
person who wrote it.

### Direct messages

You have one conversation with the bot in your direct message. Druks drops a
repeated event by its room and timestamp. The agent writes Markdown, and Druks
posts each reply in the direct message, in pieces of at most 12,000 characters.
Druks ignores the bot's own messages, edits, deletes, joins, hidden events, and
people from other workspaces.

### Files

A file you send to the bot, in a direct message or in a thread you joined,
becomes a Druks file on its message. A message with only a file starts a turn
like any other. An image reaches the agent with its message. Any other file
except audio reaches the agent as a link to a copy in the sandbox. The agent
opens the copy with its tools. An image over 25 MiB goes as a link too. An audio
clip becomes text the way a WhatsApp voice note does: see
[WhatsApp](#whatsapp). Each further file in one Slack message gets a message of
its own. The agent sends no files back.

### Rooms

Invite the bot to a channel or a group. Tag it in a message, and your own agent
answers in that message's thread. A tag inside a thread answers in that thread.
Each person who tags the bot gets their own conversation for the thread, so two
people can talk to their own agents in one thread. For 24 hours after the last
message in your conversation there, your untagged replies in the thread reach
your agent too. Other people must tag it, and a top-level message with no tag
reaches nobody. When you tag the bot before you connect Slack, the link comes as
a message in the room that only you see.

The agent reads the thread with `chat_read_thread`: the newest 200 messages,
oldest first, each with its author's id and name, whether this agent wrote it,
and whether the person it answers wrote it. Every agent in a thread posts as the
one bot, so Druks tells an agent's own replies by the Slack ids it recorded. In
a direct message the tool answers that there is no thread.

## GitHub

GitHub is a source for Chat. You tag the operator App in an issue or pull request
comment, and your own agent answers there, under your account and with your whole
toolkit. The App's slug is the tag, for example `@druks-acme`. The channel is live
while the GitHub card is connected: see [GitHub](configuration.md#github).
**Chat → Channels** shows the tag and your GitHub account.

### Link your account

Druks knows you by your GitHub account. Select **Connect GitHub** on the GitHub
pane. GitHub asks you to authorize the App, and Druks saves your GitHub sign-in
under your account, like a Gmail connection. Other apps can use that grant.
**Disconnect** revokes it.

When you tag the App before you connect, the App answers once in the thread:
connect GitHub in Druks, then tag it again. It holds nothing.

### Who the App answers

Only a comment that tags the App reaches an agent, and only when its author can
write to the repository. A comment with no tag does nothing, also when it
answers the agent. A tag on a quoted line does not count. Each person who tags
the App gets their own conversation for the issue or pull request, so two people
can talk to their own agents in one thread. Druks saves a comment once, whatever
GitHub delivers.

The agent decides what to do: answer, open a ticket, start a build, or start a
review. To review, it calls `review` and passes your comment as the note. A tag
no longer starts a review by itself. When a run the agent started fails, the
agent says so in one short line, without error details, and does not retry. Tag
it again to try again.

### Where the reply goes

The reply is a comment where the tag was: in the inline thread of a pull
request when you tagged the App there, and at the top of the issue or pull
request otherwise. Every write goes through the operator App, so every reply
posts as the App. On a public repository, anyone can read it.

The agent reads the thread with `chat_read_thread`: the issue or pull request
itself, then its newest 200 comments, oldest first, each with its author's id and
login, whether this agent wrote it, and whether the person it answers wrote it.
An inline comment also names its file and line. Every agent posts as the one
App, so Druks tells an agent's own replies by the GitHub ids it recorded. The
prompt treats only the comment of the person the agent answers as instructions.
Everything else in the thread is context.
