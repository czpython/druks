from druks.core.services import GithubApp


class GithubReviewer(GithubApp):
    """A second GitHub App. GitHub accepts its verdict reviews on pull requests
    the operator authored. Without it, reviews publish as operator comments."""

    required = False

    description = (
        "Optional: a second GitHub App, so reviews can approve pull requests. Without it, "
        "reviews publish as comments from the operator App. "
        "[Read the setup guide](https://docs.druks.ai/configuration#review-identity-optional)."
    )
    # docs/configuration.md lists these permissions — keep the two in step.
    manifest = {
        "name": "druks-reviewer",
        "description": "Druks reviewer — approves pull requests and requests changes.",
        "public": False,
        "default_permissions": {
            "metadata": "read",
            "contents": "read",
            "pull_requests": "write",
        },
    }
