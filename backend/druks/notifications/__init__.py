# Importing the package registers the runtime pieces autodiscovery doesn't
# cover: the outbox workflow must exist before DBOS.launch().
from druks.notifications import outbox  # noqa: F401
