from datetime import timedelta

# The github MCP server build ships into its own runs — build's requirement
# (there is no build without github), not an operator-facing catalog entry.
# Its token is per-repo, minted at workspace setup from the identity reviews
# act as (druks.contrib.software_factory.github).
GITHUB_MCP_NAME = "github"
GITHUB_MCP_URL = "https://api.githubcopilot.com/mcp/"
# The appliance /mcp, required when the tracker is issues. The sandbox holds a
# token limited to these tools, minted per agent call and deleted after it.
APPLIANCE_MCP_NAME = "druks"
APPLIANCE_MCP_TOOLS = (
    "software_factory_get_ticket",
    "software_factory_add_comment",
    "software_factory_set_status",
    "software_factory_update_ticket",
    "software_factory_create_ticket",
)
# The backstop for a call that dies before deleting its token.
APPLIANCE_MCP_TOKEN_LIFETIME = timedelta(days=1)
