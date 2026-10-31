Hello, I am Druks, your team's AI agent. Mention me in a channel, or write to me in a direct message.

**What I can do**
- Tell you how your work in Druks is going, answer the question a run waits on, and retry or stop a run.
{% for app in apps %}
- {{ app.description }}
{% endfor %}

**What I read**
I read the messages you send me, the thread where you mention me, and the files shared in that thread.

**What you need first**
1. Sign in to your Druks account: {{ endpoint }}
2. Connect your AI subscription: {{ endpoint }}/settings/providers
3. Connect the services that you want me to act on: {{ endpoint }}/apps/chat/settings/channels and {{ endpoint }}/settings/mcp

The first time you write to me privately, I send you a link to connect your Slack account.

**What I keep**
I keep our conversation. You can read it on the Chat page: {{ endpoint }}/chat
