# What a notification's message says in place of its buttons once its question is over.
CLOSED_MESSAGE = "This question is closed."

# What the run's account reads when the run fails and no chat reports it.
RUN_FAILED_MESSAGE = "Your run {run} failed: {failure}"

# Slack's limit for the text of all the markdown blocks of one message.
MARKDOWN_TEXT_LIMIT = 12_000

# What ends a message that Druks cut to fit, in a paragraph of its own.
TRUNCATION_MARKER = "\n\n…"

# The workflow that closes the messages of a run's question, by the name a run queues it.
CLOSE_WORKFLOW = "notifications.close"

# The workflow that closes the message of a chat agent's question, by the name a chat queues it.
CLOSE_QUESTION_WORKFLOW = "notifications.close_question"

# The workflow that sends one notification, by the name a producer queues it.
SEND_WORKFLOW = "notifications.send"

# What a person reads when they cannot answer a run of an app that a team lists.
TEAM_ONLY_MESSAGE = "Only an admin or the team that uses {app} can answer this run."
