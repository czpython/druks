# Redis keys for provider subscriptions: a pending connect flow's state while the
# operator completes it, and the per-subscription SET NX lock that serializes refresh.
CONNECT_PENDING_PREFIX = "druks:harness:connect:pending:"
REFRESH_LOCK_PREFIX = "druks:harness:refresh:"
# Provider search and details share the Models.dev cache.
DIRECTORY_CACHE_KEY = "druks:harness:directory"
DIRECTORY_CACHE_TTL_SECONDS = 24 * 3600

CLAUDE_DISALLOWED_TOOLS = (
    "CronCreate",
    "CronDelete",
    "CronList",
    "Monitor",
    "ScheduleWakeup",
)

# Every variable a harness CLI reads a provider credential from. A launch keeps only the
# variable of its own credential, because a sandbox can inject the others.
CREDENTIAL_VARIABLES = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "OPENAI_API_KEY",
    "CODEX_API_KEY",
    "CODEX_SUBSCRIPTION_TOKEN",
)
