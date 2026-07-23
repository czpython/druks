# Connect your agent

Druks serves an MCP endpoint at `/mcp` (streamable HTTP, stateless). Its
tools are derived from the agent-tagged API routes — the same five operations,
one contract: `get_gate`, `answer_gate`, `get_agent_call`, `cancel_run`,
`get_usage` — authenticated per request with a personal access token sent
as `Authorization: Bearer <token>`. Mint and revoke tokens in **Settings →
Agent access**; see
[personal access tokens](configuration.md#personal-access-tokens) for
lifecycle and compromise handling.

## Which URL

Use the public integrations host as the canonical address:
`https://druks.example.com/mcp` (the same `DRUKS_WEBHOOK_HOST` listener that
serves webhooks — see
[expose the public surfaces](../deploy/README.md#4-expose-the-public-surfaces)).
The dashboard host also serves `/mcp` in front of its identity gate, and a
[local install](full-local.md) answers at `http://127.0.0.1:8001/mcp`.

## Claude Code

```bash
claude mcp add --transport http druks https://druks.example.com/mcp --header "Authorization: Bearer <token>"
```

## Codex

Put the token in the environment (for example `export DRUKS_PAT=<token>`) and
add to `~/.codex/config.toml`:

```toml
[mcp_servers.druks]
url = "https://druks.example.com/mcp"
bearer_token_env_var = "DRUKS_PAT"
```

## What to expect

- **Polling only (v1).** There is no push channel and no discovery tool yet
  — the parked run's id arrives through the dashboard or the park
  notification. Call `get_gate` before `answer_gate` and echo its `parkedAt`
  value unchanged; it names the exact question being answered, and a repeat
  answer to the same `parkedAt` reports `already_answered` instead of
  failing.
- **Bounded responses.** Tool reads are windowed (call detail: 8KiB
  transcript tail + 4KiB stderr tail + 4KiB artifact chunk) — expect tails,
  not full payloads.
- **One error taxonomy for domain failures.** A failed tool call embeds the
  agent routes' `{"code", "message", "retryable"}` body in its error text —
  the codes (`GATE_ROUND_STALE`, `RUN_NOT_ACTIVE`, …) are stable and safe to
  match on. Requests that fail shape validation carry `VALIDATION_ERROR`
  detail instead.
