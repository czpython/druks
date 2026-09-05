# Redis keys for provider subscriptions: a pending connect flow's state while the
# operator completes it, and the per-subscription SET NX lock that serializes refresh.
CONNECT_PENDING_PREFIX = "druks:harness:connect:pending:"
REFRESH_LOCK_PREFIX = "druks:harness:refresh:"
# The models.dev directory, read when someone searches for a provider to add.
DIRECTORY_CACHE_KEY = "druks:harness:directory"
DIRECTORY_CACHE_TTL_SECONDS = 24 * 3600

CLAUDE_DISALLOWED_TOOLS = (
    "CronCreate",
    "CronDelete",
    "CronList",
    "Monitor",
    "ScheduleWakeup",
)
