# The github MCP server build ships into its own runs — build's requirement
# (there is no build without github), not an operator-facing catalog entry.
# Its token is per-repo, minted at workspace setup from the identity reviews
# act as (druks.contrib.review.github).
GITHUB_MCP_NAME = "github"
GITHUB_MCP_URL = "https://api.githubcopilot.com/mcp/"
# The appliance /mcp, required when the tracker is issues. Same doors the
# dashboard uses; the sandbox reaches them here, not through Linear.
ISSUES_MCP_NAME = "issues"
