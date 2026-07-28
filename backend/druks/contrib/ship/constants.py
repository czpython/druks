from druks.durable.enums import OPEN_STATES, RunState

# The github MCP server build ships into its own runs — build's requirement
# (there is no build without github), not an operator-facing catalog entry.
# Its token is per-repo, minted from the reviewer app at workspace setup.
GITHUB_MCP_NAME = "github"
GITHUB_MCP_URL = "https://api.githubcopilot.com/mcp/"

PLAN_DRAFTS_PER_ROUND = 2

# The board keeps an item until GitHub resolves its PR, so a finished build joins
# the states that still want the operator. A cancelled or orphaned run left nothing
# to act on, and no PR to resolve it either.
BOARD_STATES = (*OPEN_STATES, RunState.FINISHED)
