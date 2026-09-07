---
title: "API-key transport proof"
description: "How Codex, Pi, and OpenCode carry an API-key placeholder to the provider through the Drukbox secrets proxy: source evidence, controlled observations, and the open real-box checks."
---

This record establishes how Codex, Pi, and OpenCode carry an API-key
placeholder to the provider through the Drukbox secrets proxy. It separates
three kinds of evidence. Source evidence comes from the pinned versions.
Controlled observations come from the same CLIs on a workstation, with a local
proxy that answers every provider request itself. Real-box evidence comes from
a Druks sandbox. Paulo runs the real box and records that evidence here.

The proof changes no credential delivery path. The API-key follow-up reads the
selected contracts from this page.

## Versions

| Harness | Pin in `deploy/sandbox/Dockerfile` | Source read | Observed on the workstation |
| --- | --- | --- | --- |
| Codex | `@openai/codex` 0.144.1 | `openai/codex` at `rust-v0.144.1` | codex-cli 0.153.4, not the pin |
| Pi | `@earendil-works/pi-coding-agent` 0.84.4 | the npm package and `@earendil-works/pi-ai` 0.84.4 | 0.84.4 |
| OpenCode | `opencode-ai` 1.18.25 | `sst/opencode` at `v1.18.25`, `@ai-sdk/anthropic` 3.0.82, `@ai-sdk/openai` 3.0.84 | 1.18.25 |

The sandbox image installs Node 22 from NodeSource. Pi runs on that Node.
OpenCode is a Bun 1.3.14 binary. Codex is a Rust binary. The workstation ran
Node 25.9.0 on macOS, so its Pi results are local evidence only where the
runtime decides.

Druks supports two API-key providers, Anthropic and OpenAI. Codex reaches
OpenAI. Pi and OpenCode reach both.

## What the box gives a client

Drukbox exports these variables into every session of a box that holds an
entry (`ProxyInjection.put_secret` in the Drukbox repository):

| Variable | Value |
| --- | --- |
| the entry's `auth_variable` | the placeholder `drk.<host id>.<service>.<random>` |
| `HTTPS_PROXY`, `https_proxy` | the secrets proxy URL |
| `NO_PROXY` | `localhost,127.0.0.1,::1,169.254.169.254` |
| `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE` | `/etc/ssl/certs/ca-certificates.crt` |
| `NODE_EXTRA_CA_CERTS` | `/usr/local/share/ca-certificates/drukbox.crt` |

The box also installs the proxy CA into the system store with
`update-ca-certificates`, so the system bundle carries it. The proxy swaps a
placeholder only in the header its entry names, only for the entry's host.
It refuses a request whose placeholder it cannot authorize.

## Codex 0.144.1

Source evidence:

- Input. `codex exec` builds its auth with `enable_codex_api_key_env: true`
  (`codex-rs/exec/src/lib.rs`). `load_auth` in
  `codex-rs/login/src/auth/manager.rs` reads `CODEX_API_KEY` first, then
  `auth.json`. In API-key mode `auth.json` carries the key in its
  `OPENAI_API_KEY` field (`codex-rs/login/src/auth/storage.rs`). Codex does
  not read the `OPENAI_API_KEY` environment variable for authentication.
  `read_openai_api_key_from_env` serves the onboarding prefill and realtime
  conversations only.
- Destination. The default provider sends `Authorization: Bearer <key>` to
  `https://api.openai.com/v1/responses`. The Responses WebSocket transport is
  on for the default provider (`supports_websockets`,
  `codex-rs/core/src/client.rs`). Codex opens
  `wss://api.openai.com/v1/responses` first and falls back to HTTPS after a
  WebSocket failure (`force_http_fallback`). The `responses_websockets`
  feature flag has stage `Removed` in this version and no longer controls it.
- Proxy. `codex_http_client` builds every client. On Linux, system proxy
  resolution is unavailable, so the transport reads `HTTPS_PROXY`, then
  `ALL_PROXY`, and honors `NO_PROXY`
  (`codex-rs/http-client/src/outbound_proxy.rs`). The WebSocket dialer resolves
  the same route (`codex-rs/websocket-client/src/dialer.rs`).
- Trust. Codex uses rustls with the native root store and adds the bundle that
  `CODEX_CA_CERTIFICATE`, then `SSL_CERT_FILE`, names
  (`codex-rs/http-client/src/custom_ca.rs`). The WebSocket client uses the same
  configuration.

Controlled observation, codex-cli 0.153.4 on macOS:

| Input | What the proxy saw | Result |
| --- | --- | --- |
| `auth.json` with `OPENAI_API_KEY` = placeholder | `GET /v1/responses` with `Upgrade: websocket` and `Authorization: Bearer <placeholder>`, then `POST /v1/responses` with the same header | the placeholder reached the proxy on both transports |
| `CODEX_API_KEY` = placeholder | the same | the same |
| `OPENAI_API_KEY` = placeholder | the same requests with no `Authorization` header | not an input |
| `SSL_CERT_FILE` only | as the first row | trusted |
| no CA variable | `invalid peer certificate: UnknownIssuer` | refused |

The 0.153.4 binary also fetched `chatgpt.com/backend-api/plugins/featured`,
`ab.chatgpt.com`, and `api.github.com/repos/openai/plugins` through the proxy.
Those requests carry no credential.

Selected contract: a custom entry on host `api.openai.com`, variable
`CODEX_API_KEY`, header `Authorization`, prefix `Bearer `. Codex reads the
placeholder from the environment. Druks writes no `auth.json` in API-key mode.
The catalog `openai` entry does not fit, because its variable is
`OPENAI_API_KEY`, and Codex ignores that variable. The alternative, an
`auth.json` that the box writes from `$OPENAI_API_KEY`, needs a box-side write
for the same result.

## Pi 0.84.4

Source evidence:

- HTTP. `configureHttpDispatcher` in `dist/core/http-dispatcher.js` installs an
  undici `EnvHttpProxyAgent` as the global dispatcher, and `undici.install()`
  replaces the global `fetch`. Every provider SDK call honors `HTTP_PROXY`,
  `HTTPS_PROXY`, and `NO_PROXY`. Nothing overrides TLS validation, so Node's
  trust applies, and Node reads `NODE_EXTRA_CA_CERTS`. `NODE_USE_ENV_PROXY` is
  not needed.
- Anthropic, `@earendil-works/pi-ai` `providers/anthropic.js`. Credential
  order: a stored `auth.json` key, then `ANTHROPIC_AUTH_TOKEN` as
  `Authorization: Bearer`, then `ANTHROPIC_OAUTH_TOKEN` or `ANTHROPIC_API_KEY`
  as an API key. The API-key client is `@anthropic-ai/sdk` 0.91.1 with
  `apiKey`, so the header is `x-api-key`. The base URL is
  `https://api.anthropic.com`, and the call is `POST /v1/messages`.
- OpenAI, `providers/openai.js` and `api/openai-responses.js`.
  `OPENAI_API_KEY` from the environment goes to the `openai` SDK as `apiKey`,
  so the header is `Authorization: Bearer`. The base URL is
  `https://api.openai.com/v1`, and the call is `POST /v1/responses`.
- Druks writes `~/.pi/agent/auth.json` with `{type: "api_key", key}` today
  (`backend/druks/harnesses/pi.py`). A stored key wins over the environment.

Controlled observation, Pi 0.84.4 on Node 25.9.0, macOS:

| Input | What the proxy saw | Result |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` = placeholder | `POST api.anthropic.com/v1/messages`, `x-api-key: <placeholder>` | reached the proxy |
| `ANTHROPIC_AUTH_TOKEN` = placeholder | the same path, `Authorization: Bearer <placeholder>` | reached the proxy |
| `OPENAI_API_KEY` = placeholder | `POST api.openai.com/v1/responses`, `Authorization: Bearer <placeholder>` | reached the proxy |
| `NODE_EXTRA_CA_CERTS` only | as above | trusted |
| `SSL_CERT_FILE` only | as above | trusted on Node 25.9; the box runs Node 22, where `NODE_EXTRA_CA_CERTS` is the documented input |
| no CA variable | `Connection error` after three attempts | refused |

`--offline` did not stop the provider call.

Selected contracts:

- Anthropic: a custom entry on `api.anthropic.com`, variable
  `ANTHROPIC_API_KEY`, header `x-api-key`, empty prefix. This is the entry
  Claude's API-key mode already uses.
- OpenAI: the catalog `openai` entry, variable `OPENAI_API_KEY`,
  `Authorization: Bearer` on `api.openai.com`.
- Druks writes no `auth.json`. The `api_key` branch of `PiHarness.auth_file`
  goes away with the follow-up.
- The catalog `anthropic` entry, `ANTHROPIC_AUTH_TOKEN` as a bearer, is a
  working transport in Pi. Whether `api.anthropic.com` accepts an API key as a
  bearer is not established, so it is not selected.

## OpenCode 1.18.25

Source evidence:

- Keys. The provider loader takes a key from the environment variable that
  models.dev names for the provider, `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`,
  then from the auth store (`packages/opencode/src/provider/provider.ts`, the
  "load env" and "load apikeys" steps). `OPENCODE_AUTH_CONTENT` replaces the
  auth store file (`packages/opencode/src/auth/index.ts`). Druks sets it today
  (`backend/druks/harnesses/opencode.py`).
- Headers. `@ai-sdk/anthropic` 3.0.82 sends `x-api-key` and
  `anthropic-version: 2023-06-01` to `https://api.anthropic.com/v1`.
  `@ai-sdk/openai` 3.0.84 sends `Authorization: Bearer` to
  `https://api.openai.com/v1`. Both read the key from their environment
  variable when the loader passes none.
- Runtime. The npm package installs a Bun 1.3.14 binary (`packageManager` in
  the repository). OpenCode sets no proxy or TLS option itself. Bun's
  documentation describes neither `HTTPS_PROXY` nor a CA variable for `fetch`.
  The observation below is the evidence for the runtime.
- OpenCode loads provider metadata from `https://models.opencode.ai/api.json`
  at start and installs plugin packages from `registry.npmjs.org` into an empty
  home. Both go through the proxy and carry no credential.

Controlled observation, OpenCode 1.18.25 on macOS arm64:

| Input | What the proxy saw | Result |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` = placeholder | `POST api.anthropic.com/v1/messages`, `x-api-key: <placeholder>` | reached the proxy |
| `OPENAI_API_KEY` = placeholder | `POST api.openai.com/v1/responses`, `Authorization: Bearer <placeholder>` | reached the proxy |
| `OPENCODE_AUTH_CONTENT` with `{"anthropic": {"type": "api", "key": "<placeholder>"}}` | `x-api-key: <placeholder>` | reached the proxy |
| `NODE_EXTRA_CA_CERTS` only | as above | trusted |
| `SSL_CERT_FILE` only | as above | trusted |
| no CA variable | `unable to verify the first certificate` | refused |

Bun's `fetch` honored `HTTPS_PROXY` and each CA variable on its own.

Selected contracts: Anthropic through the custom `x-api-key` entry with
`ANTHROPIC_API_KEY`, OpenAI through the catalog `openai` entry. Druks stops
sending `OPENCODE_AUTH_CONTENT`. The environment is the one input.

## Open facts for the real box

Paulo runs these checks. Record the proxy's swap line, the provider's status,
and the harness output for each.

1. Codex: one completed turn through `wss://api.openai.com/v1/responses` with
   a swapped bearer, or the HTTPS fallback after the proxy refuses the upgrade.
   The swap on a WebSocket upgrade request is unproven.
2. Each harness: one completed streamed response through the proxy. The local
   proxy answered before any stream started.
3. Pi on Node 22: trust through `NODE_EXTRA_CA_CERTS`.
4. OpenCode: the `models.opencode.ai` and `registry.npmjs.org` fetches through
   the proxy, or their failure mode when the box blocks them.

No Drukbox capability is missing for the selected contracts. Custom entries and
the `openai` catalog entry cover every proven shape. A failed check on item 1
opens a Drukbox issue.

## Repeat the controlled observation

`scripts/proof/api_key_transport.sh` runs each harness with a placeholder
through a local mitmproxy that answers every provider request with 401 and
logs the host, path, and credential headers. It needs `uvx` and the three CLIs
on `PATH`, and no provider credential. Each CLI gets an empty home, the proxy
variables the box gets, and the prompt on stdin.
