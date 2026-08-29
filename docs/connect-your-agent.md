---
title: "Connect your agent"
description: "Connect Claude Code or Codex to the Druks MCP endpoint with a personal access token."
icon: "plug"
---

Druks serves a stateless, streamable HTTP endpoint at `/mcp`. Its tools come
from the agent-tagged API routes. The platform contributes seven tools:

- `list_open_subjects`
- `get_gate`
- `answer_gate`
- `get_agent_call`
- `cancel_run`
- `retry_run`
- `get_usage`.

Each installed app can add its own tools. `tools/list` is the live catalog.
Each request uses a personal access token in
`Authorization: Bearer <token>`. Mint and revoke tokens in
**Settings → Tokens**. See
[personal access tokens](configuration.md#personal-access-tokens) for token
lifecycle and incident steps.

## Which URL

Use the public integrations host as the canonical address:
`https://druks.example.com/mcp`. The `DRUKS_WEBHOOK_HOST` listener also serves
webhooks. See
[expose the public surfaces](deployment.md#4-expose-the-public-surfaces).
The dashboard host also serves `/mcp` in front of its identity gate. A
[local install](full-local.md) answers at `http://127.0.0.1:8001/mcp`.

## Claude Code

```bash
claude mcp add --transport http druks https://druks.example.com/mcp --header "Authorization: Bearer <token>"
```

## Codex

Put the token in the environment. For example, use
`export DRUKS_PAT=<token>`. Then add this configuration to
`~/.codex/config.toml`:

```toml
[mcp_servers.druks]
url = "https://druks.example.com/mcp"
bearer_token_env_var = "DRUKS_PAT"
```

## What to expect

Three details matter when an agent uses the MCP endpoint:

- **Discovery first.** There is no push channel. The agent calls `list_open_subjects`
  first. It polls approximately every 30 seconds during a wait. Each workflow
  `run` supplies the gate and run tools. `latestAgentCall` supplies
  `get_agent_call`.

  An app tool opens work that enters the same flow. The agent calls
  `get_gate` before `answer_gate`. It copies the `parkedAt` value without a change.
  This value identifies the exact question. A second answer for the same
  `parkedAt` returns `already_answered`.
- **Bounded responses.** Tool reads use fixed windows. Call detail contains an
  8KiB transcript tail, a 4KiB stderr tail, and a 4KiB artifact section. These
  values are tails, not full payloads.
- **Stable error shapes.** Gateway and run tool errors contain the agent-route
  body `{"code", "message", "retryable"}`. Codes such as `GATE_ROUND_STALE`
  and `RUN_NOT_ACTIVE` are stable match values. App tools use the API shape
  `{"error", "detail"}` for refusals. Shape errors contain
  `VALIDATION_ERROR` detail.
