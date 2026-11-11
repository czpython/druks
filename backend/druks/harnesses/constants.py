# Redis keys for provider subscriptions: a pending connect flow's state while the
# operator completes it, and the per-subscription SET NX lock that serializes refresh.
CONNECT_PENDING_PREFIX = "druks:harness:connect:pending:"
REFRESH_LOCK_PREFIX = "druks:harness:refresh:"
# Provider search and details share the Models.dev cache.
DIRECTORY_CACHE_KEY = "druks:harness:directory"
DIRECTORY_CACHE_TTL_SECONDS = 24 * 3600

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
