from druks.mcp.constants import DRUKS_SERVER_NAME
from druks.secrets.datastructures import Audience

# The dot keeps the name outside NAME_PATTERN, so no registry server can share its vault row.
CHAT_AUDIENCE = Audience.mcp(f"{DRUKS_SERVER_NAME}.chat")
CHAT_BRIDGE_PORT = 43123
CHAT_LOCK_TTL_SECONDS = 30
