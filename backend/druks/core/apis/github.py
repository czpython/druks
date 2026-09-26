import base64
import functools
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, TypeVar

from githubkit import AppAuthStrategy, AppInstallationAuthStrategy, GitHub
from githubkit.exception import GraphQLFailed, RequestFailed

from druks.core.apis.exceptions import GitHubAppNotInstalledError
from druks.core.utils.time import ensure_utc

logger = logging.getLogger(__name__)

GITHUB = "github"
# The authority of a GitHub sign-in: one GitHub, so a user id names one person.
GITHUB_AUTHORITY = "https://github.com"


@dataclass(frozen=True)
class ReviewComment:
    path: str
    line: int
    body: str
    start_line: int | None = None


# Where-druks-may-act is derived from the App's installations. Module-level
# so it survives the per-call-site client construction; keyed by app id;
# serves the last-known set when GitHub hiccups.
_INSTALLATION_ACCOUNTS_TTL_SECONDS = 600.0
_INSTALLATION_ACCOUNTS_CACHE: dict[str, tuple[float, tuple[str, ...]]] = {}

# An App's slug and bot user id are fixed for its life, so these need no expiry.
_MENTION_HANDLE_CACHE: dict[str, str] = {}
_BOT_USER_ID_CACHE: dict[str, int] = {}


F = TypeVar("F", bound=Callable[..., Awaitable[Any]])


def _retry_on_401(func: F) -> F:
    @functools.wraps(func)
    async def wrapper(self: "GitHubClient", repo: str, *args: Any, **kwargs: Any) -> Any:
        try:
            return await func(self, repo, *args, **kwargs)
        except RequestFailed as error:
            if error.response.status_code != 401:
                raise
            logger.warning(
                "GitHub 401 on %s/%s for %s; dropping cached installation client and retrying once",
                repo,
                func.__name__,
                repo,
            )
            await self._invalidate_for_repo(repo)
            return await func(self, repo, *args, **kwargs)

    return wrapper  # type: ignore[return-value]


class GitHubClient:
    def __init__(
        self,
        *,
        app_id: str,
        private_key: str,
        base_url: str = "https://api.github.com",
        slug: str = "",
    ) -> None:
        self._app_id = app_id
        self._private_key = private_key
        self._base_url = base_url
        self._slug = slug
        self._app = GitHub(
            AppAuthStrategy(app_id, private_key),
            base_url=base_url,
        )
        self._installation_cache: dict[str, int] = {}
        self._repo_gh_cache: dict[int, GitHub] = {}

    async def aclose(self) -> None:
        # With direct method calls githubkit creates and closes a fresh httpx
        # client per request, so there is no persistent pool to release. Dropping
        # the per-installation cache is the only cleanup. Pooled connections
        # would require entering the client context and closing it here.
        self._repo_gh_cache.clear()

    async def _owner_installation_id(self, owner: str) -> int:
        try:
            response = await self._app.rest.apps.async_get_org_installation(owner)
        except RequestFailed as error:
            if error.response.status_code != 404:
                raise
            try:
                response = await self._app.rest.apps.async_get_user_installation(owner)
            except RequestFailed as error:
                if error.response.status_code == 404:
                    raise GitHubAppNotInstalledError(owner) from error
                raise
        return response.parsed_data.id

    async def list_repos_for_owner(self, owner: str) -> list[dict[str, Any]]:
        """Repos the GitHub App can see under ``owner``.

        Returns ``[{"full_name": "<owner>/<name>", "description": ...}, ...]``.
        Empty when the app isn't installed on this owner or has no repo
        access. Used by the projects UI as the typeahead source for the
        "add repo" affordance.
        """
        try:
            installation_id = await self._owner_installation_id(owner)
        except GitHubAppNotInstalledError:
            return []

        async with GitHub(
            AppInstallationAuthStrategy(self._app_id, self._private_key, installation_id),
            base_url=self._base_url,
        ) as github:
            repos: list[dict[str, Any]] = []
            page = 1
            while True:
                response = await github.rest.apps.async_list_repos_accessible_to_installation(
                    per_page=100,
                    page=page,
                )
                items = response.parsed_data.repositories
                if not items:
                    break
                for repository in items:
                    repos.append(
                        {
                            "full_name": repository.full_name,
                            "description": repository.description,
                        }
                    )
                if len(items) < 100:
                    break
                page += 1
            return repos

    async def create_repo_from_template(self, owner: str, template_repo: str, name: str) -> str:
        """A new private repo ``name`` under ``owner``, generated from the
        ``owner/name`` template ``template_repo``; returns its ``full_name``.
        Safe to retry: a name already taken reads as already created."""
        installation_id = await self._owner_installation_id(owner)
        template_owner, template_name = template_repo.split("/", 1)
        full_name = f"{owner}/{name}"
        async with GitHub(
            AppInstallationAuthStrategy(self._app_id, self._private_key, installation_id),
            base_url=self._base_url,
        ) as github:
            try:
                await github.rest.repos.async_create_using_template(
                    template_owner,
                    template_name,
                    name=name,
                    owner=owner,
                    private=True,
                )
            except RequestFailed as error:
                # On a valid name GitHub's 422 is "already taken" — a retried create.
                if error.response.status_code == 422:
                    return full_name
                raise
        return full_name

    async def list_installation_accounts(self) -> tuple[str, ...]:
        """Account logins (orgs/users) this App is installed on — the
        authoritative "where druks may act". Install the App somewhere and
        druks works there; uninstall and it stops. Cached ~10 min per app
        id, serving the last-known set when GitHub hiccups."""
        cached = _INSTALLATION_ACCOUNTS_CACHE.get(self._app_id)
        now = time.monotonic()
        if cached and now - cached[0] < _INSTALLATION_ACCOUNTS_TTL_SECONDS:
            return cached[1]
        try:
            accounts: list[str] = []
            page = 1
            while True:
                response = await self._app.rest.apps.async_list_installations(
                    per_page=100, page=page
                )
                batch = response.parsed_data
                accounts.extend(
                    str(login)
                    for installation in batch
                    if (login := getattr(installation.account, "login", None))
                )
                if len(batch) < 100:
                    break
                page += 1
        except Exception:
            if cached:
                logger.warning(
                    "Could not refresh App installations; serving the last-known set.",
                    exc_info=True,
                )
                return cached[1]
            raise
        result = tuple(dict.fromkeys(accounts))
        _INSTALLATION_ACCOUNTS_CACHE[self._app_id] = (now, result)
        return result

    async def get_mention_handle(self) -> str:
        """What a comment writes after ``@`` to address this App — its slug, which
        GitHub resolves to the App's bot user. The operator client carries the
        slug stored at connect time and never refetches it; a client constructed
        without one (the review identity) asks GitHub once and caches it."""
        if self._slug:
            return self._slug
        cached = _MENTION_HANDLE_CACHE.get(self._app_id)
        if cached:
            return cached
        response = await self._app.rest.apps.async_get_authenticated()
        handle = getattr(response.parsed_data, "slug", None) or ""
        _MENTION_HANDLE_CACHE[self._app_id] = handle
        return handle

    async def get_bot_git_author(self) -> tuple[str, str]:
        """Name and email for commits made as the App's bot user — the
        ``<id>+<slug>[bot]@users.noreply.github.com`` convention, the same
        identity a squash-merge advertises. The id comes from the public users
        endpoint, no auth needed."""
        bot_name = f"{await self.get_mention_handle()}[bot]"
        user_id = _BOT_USER_ID_CACHE.get(self._app_id)
        if not user_id:
            async with GitHub(base_url=self._base_url) as github:
                response = await github.rest.users.async_get_by_username(bot_name)
            user_id = response.parsed_data.id
            _BOT_USER_ID_CACHE[self._app_id] = user_id
        return bot_name, f"{user_id}+{bot_name}@users.noreply.github.com"

    async def get_authenticated_app_slug(self) -> str:
        """The slug GitHub reports for these credentials — a live App-JWT call,
        so it proves the App ID matches the PEM. The connect flow's validation;
        mention resolution reads the stored slug instead."""
        async with GitHub(
            AppAuthStrategy(self._app_id, self._private_key),
            base_url=self._base_url,
        ) as github:
            response = await github.rest.apps.async_get_authenticated()
            return str(getattr(response.parsed_data, "slug", None) or "")

    async def _installation_id(self, repo: str) -> int:
        if repo in self._installation_cache:
            return self._installation_cache[repo]
        owner, name = repo.split("/", 1)
        try:
            response = await self._app.rest.apps.async_get_repo_installation(owner, name)
        except RequestFailed as error:
            if error.response.status_code == 404:
                raise GitHubAppNotInstalledError(repo) from error
            raise
        installation_id: int = response.parsed_data.id
        self._installation_cache[repo] = installation_id
        return installation_id

    async def _for_repo(self, repo: str) -> GitHub:
        installation_id = await self._installation_id(repo)
        if installation_id not in self._repo_gh_cache:
            self._repo_gh_cache[installation_id] = GitHub(
                AppInstallationAuthStrategy(
                    self._app_id,
                    self._private_key,
                    installation_id,
                ),
                base_url=self._base_url,
            )
        return self._repo_gh_cache[installation_id]

    async def _invalidate_for_repo(self, repo: str) -> None:
        installation_id = self._installation_cache.pop(repo, None)
        github = self._repo_gh_cache.pop(installation_id, None) if installation_id else None
        if github:
            try:
                await github.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001 — best-effort cleanup, log and move on
                logger.warning(
                    "Failed to close stale GitHub client for %s; leaking httpx pool",
                    repo,
                    exc_info=True,
                )

    @_retry_on_401
    async def token_for_repo(self, repo: str) -> tuple[str, datetime]:
        """The installation token for ``repo`` and the expiry GitHub gave it."""
        # The decorator drops the cached installation client + id on a
        # 401 and retries once. Important here because git is the
        # consumer of the minted token — once it's handed to git,
        # there's no httpx-layer retry hook to recover from a stale
        # ``installation_id`` (e.g. after the App was reinstalled and
        # got a new id, leaving every worker's cache pointing at the
        # dead one). The 401 from this method's own SDK call gives us
        # the only chance to invalidate before git presents a bad
        # token to GitHub and produces ``expected flush after ref
        # listing``.
        installation_id = await self._installation_id(repo)
        token_response = await self._app.rest.apps.async_create_installation_access_token(
            installation_id,
        )
        token = token_response.parsed_data
        return str(token.token), ensure_utc(datetime.fromisoformat(str(token.expires_at)))

    @_retry_on_401
    async def get_repository(self, repo: str) -> dict[str, Any]:
        owner, name = repo.split("/", 1)
        github = await self._for_repo(repo)
        response = await github.rest.repos.async_get(owner, name)
        return response.parsed_data.model_dump()

    @_retry_on_401
    async def get_issue(self, repo: str, issue_number: int) -> dict[str, Any]:
        owner, name = repo.split("/", 1)
        github = await self._for_repo(repo)
        response = await github.rest.issues.async_get(owner, name, issue_number)
        return response.parsed_data.model_dump()

    @_retry_on_401
    async def list_comments(self, repo: str, issue_number: int) -> list[dict[str, Any]]:
        """Every top-level comment on the issue or pull request, oldest first."""
        owner, name = repo.split("/", 1)
        github = await self._for_repo(repo)
        comments = github.rest.paginate(
            github.rest.issues.async_list_comments,
            owner=owner,
            repo=name,
            issue_number=issue_number,
            per_page=100,
        )
        return [comment.model_dump() async for comment in comments]

    @_retry_on_401
    async def list_review_comments(self, repo: str, pr_number: int) -> list[dict[str, Any]]:
        """Every inline comment on the pull request, oldest first."""
        owner, name = repo.split("/", 1)
        github = await self._for_repo(repo)
        comments = github.rest.paginate(
            github.rest.pulls.async_list_review_comments,
            owner=owner,
            repo=name,
            pull_number=pr_number,
            per_page=100,
        )
        return [comment.model_dump() async for comment in comments]

    @_retry_on_401
    async def create_comment(self, repo: str, issue_number: int, body: str) -> dict[str, Any]:
        owner, name = repo.split("/", 1)
        github = await self._for_repo(repo)
        response = await github.rest.issues.async_create_comment(
            owner, name, issue_number, body=body
        )
        return response.parsed_data.model_dump()

    @_retry_on_401
    async def reply_to_review_comment(
        self, repo: str, pr_number: int, comment_id: int, body: str
    ) -> dict[str, Any]:
        """A reply in the inline thread that ``comment_id`` starts. GitHub takes the
        thread's first comment only, never a reply in it."""
        owner, name = repo.split("/", 1)
        github = await self._for_repo(repo)
        response = await github.rest.pulls.async_create_reply_for_review_comment(
            owner, name, pr_number, comment_id, body=body
        )
        return response.parsed_data.model_dump()

    @_retry_on_401
    async def get_pull_request(self, repo: str, pr_number: int) -> dict[str, Any]:
        owner, name = repo.split("/", 1)
        github = await self._for_repo(repo)
        response = await github.rest.pulls.async_get(owner, name, pr_number)
        return response.parsed_data.model_dump()

    @_retry_on_401
    async def squash_merge_pull_request(self, repo: str, pr_number: int) -> bool:
        # No commit_title: GitHub then titles the squash commit from the PR title.
        owner, name = repo.split("/", 1)
        github = await self._for_repo(repo)
        try:
            response = await github.rest.pulls.async_merge(
                owner,
                name,
                pr_number,
                data={"merge_method": "squash"},
            )
        except RequestFailed as error:
            if error.response.status_code in {405, 409}:
                return False
            raise
        return response.parsed_data.merged

    @_retry_on_401
    async def enable_auto_merge(self, repo: str, node_id: str) -> bool:
        github = await self._for_repo(repo)
        try:
            await github.async_graphql(
                """
                mutation EnablePullRequestAutoMerge($id: ID!) {
                  enablePullRequestAutoMerge(
                    input: {pullRequestId: $id, mergeMethod: SQUASH}
                  ) {
                    pullRequest { id }
                  }
                }
                """,
                {"id": node_id},
            )
        except GraphQLFailed:
            return False
        return True

    @_retry_on_401
    async def update_pull_request_branch(self, repo: str, pr_number: int) -> None:
        owner, name = repo.split("/", 1)
        github = await self._for_repo(repo)
        try:
            await github.rest.pulls.async_update_branch(owner, name, pr_number)
        except RequestFailed as error:
            if error.response.status_code == 422:
                return
            raise

    async def merge_when_ready(self, repo: str, pr_number: int) -> bool:
        """Whether GitHub accepted ownership of the merge."""
        pull_request = await self.get_pull_request(repo, pr_number)
        if pull_request["state"] == "closed":
            return True

        # A clean pull request merges directly: GitHub refuses to arm auto-merge
        # on one, and updating its branch first moves the head asynchronously,
        # racing an immediate merge into a 409.
        if await self.squash_merge_pull_request(repo, pr_number):
            return True
        if await self.enable_auto_merge(repo, pull_request["node_id"]):
            await self.update_pull_request_branch(repo, pr_number)
            return True
        return False

    @_retry_on_401
    async def update_pull_request_body(
        self,
        repo: str,
        pr_number: int,
        body: str,
    ) -> dict[str, Any]:
        owner, name = repo.split("/", 1)
        github = await self._for_repo(repo)
        response = await github.rest.pulls.async_update(owner, name, pr_number, body=body)
        return response.parsed_data.model_dump()

    @_retry_on_401
    async def set_pull_request_draft_state(
        self,
        repo: str,
        pr_number: int,
        *,
        draft: bool,
    ) -> None:
        pull_request = await self.get_pull_request(repo, pr_number)
        if bool(pull_request.get("draft")) == draft:
            return

        node_id = str(pull_request["node_id"])
        github = await self._for_repo(repo)
        if draft:
            await github.async_graphql(
                """
                mutation ConvertPullRequestToDraft($id: ID!) {
                  convertPullRequestToDraft(input: {pullRequestId: $id}) {
                    pullRequest { id }
                  }
                }
                """,
                {"id": node_id},
            )
            return

        await github.async_graphql(
            """
            mutation MarkPullRequestReadyForReview($id: ID!) {
              markPullRequestReadyForReview(input: {pullRequestId: $id}) {
                pullRequest { id }
              }
            }
            """,
            {"id": node_id},
        )

    @_retry_on_401
    async def delete_branch(self, repo: str, branch: str) -> bool:
        owner, name = repo.split("/", 1)
        github = await self._for_repo(repo)
        try:
            await github.rest.git.async_delete_ref(owner, name, f"heads/{branch}")
        except RequestFailed as error:
            if error.response.status_code == 404:
                return False
            raise

        return True

    @_retry_on_401
    async def request_pull_request_reviewers(
        self,
        repo: str,
        pr_number: int,
        reviewers: list[str],
    ) -> None:
        owner, name = repo.split("/", 1)
        github = await self._for_repo(repo)
        await github.rest.pulls.async_request_reviewers(
            owner,
            name,
            pr_number,
            reviewers=reviewers,
        )

    @_retry_on_401
    async def create_review(
        self,
        repo: str,
        pr_number: int,
        *,
        event: Literal["APPROVE", "REQUEST_CHANGES", "COMMENT"],
        body: str,
        comments: list[ReviewComment] | None = None,
    ) -> dict[str, Any]:
        owner, name = repo.split("/", 1)
        github = await self._for_repo(repo)
        raw_comments = _build_review_comments(comments) if comments else None
        try:
            kwargs: dict[str, Any] = {
                "event": event,
                "body": body,
            }
            if raw_comments:
                kwargs["comments"] = raw_comments
            response = await github.rest.pulls.async_create_review(
                owner,
                name,
                pr_number,
                **kwargs,
            )
            return response.parsed_data.model_dump()
        except RequestFailed as error:
            if error.response.status_code != 422 or not raw_comments:
                raise
            logger.warning(
                "Review with inline comments rejected (422) for %s#%d; retrying body-only.",
                repo,
                pr_number,
            )
            fallback_body = _fold_comments_into_body(body, comments or [])
            response = await github.rest.pulls.async_create_review(
                owner,
                name,
                pr_number,
                event=event,
                body=fallback_body,  # type: ignore[arg-type]
            )
            return response.parsed_data.model_dump()

    @_retry_on_401
    async def get_file_content(
        self,
        repo: str,
        path: str,
        *,
        ref: str | None = None,
    ) -> str | None:
        owner, name = repo.split("/", 1)
        try:
            # _for_repo inside the try: an unreachable repo fails at the installation
            # lookup, which for an optional-file read is also "no such file".
            github = await self._for_repo(repo)
            response = await github.rest.repos.async_get_content(
                owner,
                name,
                path,
                ref=ref or "",
            )
        except GitHubAppNotInstalledError:
            return
        except RequestFailed as error:
            if error.response.status_code == 404:
                return
            raise
        data = response.parsed_data
        content = getattr(data, "content", None) or ""
        return base64.b64decode(content).decode()

    @_retry_on_401
    async def download_tarball(self, repo: str) -> bytes:
        """Gzipped tarball of ``repo``'s default branch."""
        owner, name = repo.split("/", 1)
        github = await self._for_repo(repo)
        response = await github.rest.repos.async_download_tarball_archive(owner, name, "")
        return response.content


async def download_public_tarball(repo: str) -> bytes:
    """Gzipped tarball of a *public* repo's default branch.
    Fetched anonymously — no App installation, unlike ``GitHubClient`` whose every
    call resolves an installation token and so can't reach repos druks isn't on."""
    owner, name = repo.split("/", 1)
    async with GitHub() as github:
        response = await github.rest.repos.async_download_tarball_archive(owner, name, "")
        return response.content


def _build_review_comments(comments: list[ReviewComment]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for comment in comments:
        entry: dict[str, Any] = {"path": comment.path, "line": comment.line, "body": comment.body}
        if comment.start_line:
            entry["start_line"] = comment.start_line
        result.append(entry)
    return result


def _fold_comments_into_body(body: str, comments: list[ReviewComment]) -> str:
    lines = [body, "", "---", ""]
    for comment in comments:
        location = f"`{comment.path}:{comment.line}`"
        if comment.start_line:
            location = f"`{comment.path}:{comment.start_line}-{comment.line}`"
        lines.append(f"**{location}**\n{comment.body}\n")
    return "\n".join(lines)
