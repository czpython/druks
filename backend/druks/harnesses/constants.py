from datetime import timedelta

# Redis keys for provider subscriptions: a pending connect flow's state while the
# operator completes it, and the per-subscription SET NX lock that serializes refresh.
CONNECT_PENDING_PREFIX = "druks:harness:connect:pending:"
REFRESH_LOCK_PREFIX = "druks:harness:refresh:"
# Provider search and details share the Models.dev cache.
DIRECTORY_CACHE_KEY = "druks:harness:directory"
DIRECTORY_CACHE_TTL_SECONDS = 24 * 3600
# How long before a subscription's login expires its account is warned.
LOGIN_WARNING_MARGIN = timedelta(days=3)
# The reason of the notification that warns an account before its login expires.
LOGIN_EXPIRING_REASON = "subscription.login_expiring"
# What an account reads when its subscription disconnects, and before its login expires.
SUBSCRIPTION_DISCONNECTED_MESSAGE = (
    "Your {provider} subscription is disconnected. Reconnect it in Settings → Providers."
)
LOGIN_EXPIRING_MESSAGE = (
    "Your {provider} login expires on {expires_at:%d %b %Y at %H:%M %Z}. "
    "Reconnect it in Settings → Providers before then."
)

# Appended to the system prompt of every structured Claude run. Without it, an agent
# often wraps its result in another key, or passes a list as JSON text; Claude Code
# rejects the call and the agent writes its whole result again. Measured with
# claude-opus-4-8: 6 of 16 runs resubmitted without it, 0 of 16 with it.
CLAUDE_STRUCTURED_OUTPUT_INSTRUCTION = (
    "Return your result by calling the StructuredOutput tool once. Its input is the "
    "result itself: put each field of the schema at the top level of the input, and give "
    "each value its JSON type, a list as an array and an object as an object. Do not wrap "
    "the result in another key, and do not pass the result, or any field of it, as a "
    "string of JSON."
)

CLAUDE_DISALLOWED_TOOLS = (
    "CronCreate",
    "CronDelete",
    "CronList",
    "Monitor",
    "ScheduleWakeup",
)
