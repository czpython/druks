# The github MCP server build ships into its own runs — build's requirement
# (there is no build without github), not an operator-facing catalog entry.
# Its token is per-repo, minted at workspace setup from the identity reviews
# act as (druks.contrib.software_factory.github).
GITHUB_MCP_NAME = "github"
GITHUB_MCP_URL = "https://api.githubcopilot.com/mcp/"
TICKET_TOOLS = (
    "software_factory_get_ticket",
    "software_factory_add_comment",
    "software_factory_set_status",
    "software_factory_update_ticket",
    "software_factory_create_ticket",
)
# A review takes minutes. A build still waiting after this long was never answered.
REVIEW_TTL_SECONDS = 2 * 60 * 60
# A repo with CI registers a check within this long of a push. With none by then, the
# repo has no CI, and the build goes on as if CI were green.
CI_REGISTER_SECONDS = 3 * 60
# CI still running after this long goes to the review unfinished.
CI_TTL_SECONDS = 60 * 60
# A webhook wakes the CI wait early. The poll catches a check that finished before the
# wait began, and a delivery that never came.
CI_POLL_SECONDS = 60
# A suite that passed or did not apply.
CI_GREEN_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})
