from druks.mcp.constants import DRUKS_SERVER_NAME

# Workflows enqueue here; execution distributes across whichever processes
# launched DBOS. One queue until a unit earns its own policy.
RUN_QUEUE = "druks"
TASK_QUEUE = "druks_tasks"
# A pause holds a chat for hours, so it has its own queue, apart from runs.
PAUSE_QUEUE = "druks_chat_pauses"
# Delivery retries on its own schedule, fully decoupled from run lifecycles.
NOTIFICATIONS_QUEUE = "druks_notifications"
# The failure a run records when a newer run replaces it.
REPLACED_FAILURE = "A newer run replaced this run."
# The failure a run records when Druks cancels it, still parked after its ask expired.
STALE_PARKED_FAILURE = "No worker ran this run after its ask expired."
# A person's schedules fire through DBOS schedules with this name prefix, beside the
# schedules that workflows and tasks declare.
SCHEDULE_NAME_PREFIX = "schedule:"
RUN_SCHEDULE_WORKFLOW = "druks.run_schedule"
# The name of the key a schedule calls its tool with.
SCHEDULE_KEY_NAME = f"{DRUKS_SERVER_NAME}.schedules"
