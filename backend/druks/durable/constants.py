# Workflows enqueue here; execution distributes across whichever processes
# launched DBOS. One queue until a unit earns its own policy.
RUN_QUEUE = "druks"
TASK_QUEUE = "druks_tasks"
# A pause holds a chat for hours, so it has its own queue, apart from runs.
PAUSE_QUEUE = "druks_chat_pauses"
# Delivery retries on its own schedule, fully decoupled from run lifecycles.
NOTIFICATIONS_QUEUE = "druks_notifications"
