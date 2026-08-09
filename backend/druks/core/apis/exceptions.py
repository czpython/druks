class GitHubAppNotInstalledError(Exception):
    """The GitHub App has no installation covering the repo — it was never
    installed there, the repo isn't in the installation's selected
    repositories, or the repo doesn't exist. The message names the repo so a
    run failure surfaces the actionable cause, not githubkit's response repr."""

    def __init__(self, repo: str) -> None:
        super().__init__(
            f"The GitHub App has no access to {repo} — install the app on the "
            "repo (or add it to the installation's selected repositories)."
        )
        self.repo = repo
