import re

# The bearer token for a server rides in an env var derived from its name — only
# the var name ever lands in emitted config, never the value.
TOKEN_ENV_PREFIX = "MCP_"
TOKEN_ENV_SUFFIX = "_TOKEN"

# A server name is one identifier reused as the MCP config key (a bare TOML path
# segment for codex, a JSON object key for claude) and the stem of the bearer env
# var — a lowercase shell/TOML-safe token, letter-led so no rendering breaks and
# two names never collapse to one env var.
NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")

# The official MCP registry the picker resolves against; search results
# cache briefly in Redis so typing in the picker doesn't hammer it.
REGISTRY_SEARCH_URL = "https://registry.modelcontextprotocol.io/v0/servers"
REGISTRY_SEARCH_CACHE_PREFIX = "mcp:registry:search:"
REGISTRY_CACHE_TTL_SECONDS = 300

# OAuth connect + mint plumbing. The callback path is public API surface — the
# authorization server redirects the operator's browser to
# {DRUKS_ENDPOINT}{OAUTH_CALLBACK_PATH} after consent. Access tokens cache in
# Redis under the token key prefix for their lifetime minus the skew (so a
# token injected into a run never expires moments after delivery); pending
# connect state (PKCE verifier + endpoints) lives under the connect prefix for
# its short TTL, single-use.
OAUTH_CALLBACK_PATH = "/api/mcp-servers/oauth/callback"
OAUTH_CONNECT_STATE_PREFIX = "mcp:oauth:connect:"
OAUTH_ACCESS_TOKEN_PREFIX = "mcp:oauth:access_token:"
OAUTH_CONNECT_STATE_TTL_SECONDS = 600
OAUTH_TOKEN_TTL_SKEW_SECONDS = 60

# Mint's mutual exclusion, in the Redis that fronts the token cache (the run
# lock's SET NX idiom): a rotating grant tolerates exactly one refresher per
# server. The lock TTL is a crash backstop at three times the HTTP client's
# timeout — a live refresh cannot outlive it. Losers poll the cache on the
# interval for about one token-endpoint round trip, then fail loudly.
OAUTH_REFRESH_LOCK_PREFIX = "mcp:oauth:refresh_lock:"
OAUTH_REFRESH_LOCK_TTL_SECONDS = 90
OAUTH_MINT_WAIT_INTERVAL_SECONDS = 0.2
OAUTH_MINT_WAIT_ATTEMPTS = 150
