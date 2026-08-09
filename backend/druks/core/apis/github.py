import base64
import functools
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, TypeVar

from githubkit import AppAuthStrategy, AppInstallationAuthStrategy, GitHub
from githubkit.exception import GraphQLFailed, RequestFailed

from druks.core.apis.exceptions import GitHubAppNotInstalledError
from druks.services.models import ServiceIdentity
from druks.settings import load_settings

logger = logging.getLogger(__name__)

GITHUB = "github"


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

# An App's slug is fixed for the life of the App, so this needs no expiry.
_MENTION_HANDLE_CACHE: dict[str, str] = {}


F = TypeVar("F", bound=Callable[..., Awaitable[Any]])


def _retry_on_401(func: F) -> F:
    @functools.wraps(func)
    async def wrapper(self: "GitHubClient", repo: str, *args: Any, **kwargs: Any) -> Any:
        try:
            return await func(self, repo, *args, **kwargs)
        except RequestFailed as exc:
            if exc.response.status_code != 401:
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

    async def list_repos_for_owner(self, owner: str) -> list[dict[str, Any]]:
        """Repos the GitHub App can see under ``owner``.

        Returns ``[{"full_name": "<owner>/<name>", "description": ...}, ...]``.
        Empty when the app isn't installed on this owner or has no repo
        access. Used by the projects UI as the typeahead source for the
        "add repo" affordance.
        """
        try:
            installation_response = await self._app.rest.apps.async_get_org_installation(owner)
        except RequestFailed as error:
            if error.response.status_code != 404:
                raise
            try:
                installation_response = await self._app.rest.apps.async_get_user_installation(owner)
            except RequestFailed as error:
                if error.response.status_code == 404:
                    return []
                raise
        installation_id = installation_response.parsed_data.id

        async with GitHub(
            AppInstallationAuthStrategy(self._app_id, self._private_key, installation_id),
            base_url=self._base_url,
        ) as gh:
            repos: list[dict[str, Any]] = []
            page = 1
            while True:
                response = await gh.rest.apps.async_list_repos_accessible_to_installation(
                    per_page=100,
                    page=page,
                )
                items = response.parsed_data.repositories
                if not items:
                    break
                for r in items:
                    repos.append(
                        {
                            "full_name": r.full_name,
                            "description": r.description,
                        }
                    )
                if len(items) < 100:
                    break
                page += 1
            return repos

    async def list_installation_accounts(self) -> tuple[str, ...]:
        """Account logins (orgs/users) this App is installed on — the
        authoritative "where druks may act". Install the App somewhere and
        druks works there; uninstall and it stops. Cached ~10 min per app
        id, serving the last-known set when GitHub hiccups."""
        cached = _INSTALLATION_ACCOUNTS_CACHE.get(self._app_id)
        now = time.monotonic()
        if cached is not None and now - cached[0] < _INSTALLATION_ACCOUNTS_TTL_SECONDS:
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
                    str(login) for inst in batch if (login := getattr(inst.account, "login", None))
                )
                if len(batch) < 100:
                    break
                page += 1
        except Exception:
            if cached is not None:
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

    async def get_authenticated_app_slug(self) -> str:
        """The slug GitHub reports for these credentials — a live App-JWT call,
        so it proves the App ID matches the PEM. The connect flow's validation;
        mention resolution reads the stored slug instead."""
        async with GitHub(
            AppAuthStrategy(self._app_id, self._private_key),
            base_url=self._base_url,
        ) as gh:
            response = await gh.rest.apps.async_get_authenticated()
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
        inst_id: int = response.parsed_data.id
        self._installation_cache[repo] = inst_id
        return inst_id

    async def _for_repo(self, repo: str) -> GitHub:
        inst_id = await self._installation_id(repo)
        if inst_id not in self._repo_gh_cache:
            self._repo_gh_cache[inst_id] = GitHub(
                AppInstallationAuthStrategy(
                    self._app_id,
                    self._private_key,
                    inst_id,
                ),
                base_url=self._base_url,
            )
        return self._repo_gh_cache[inst_id]

    async def _invalidate_for_repo(self, repo: str) -> None:
        inst_id = self._installation_cache.pop(repo, None)
        if inst_id is None:
            return
        gh = self._repo_gh_cache.pop(inst_id, None)
        if gh is not None:
            try:
                await gh.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001 — best-effort cleanup, log and move on
                logger.warning(
                    "Failed to close stale GitHub client for %s; leaking httpx pool",
                    repo,
                    exc_info=True,
                )

    @_retry_on_401
    async def token_for_repo(self, repo: str) -> str:
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
        token_resp = await self._app.rest.apps.async_create_installation_access_token(
            installation_id,
        )
        return str(token_resp.parsed_data.token)

    @_retry_on_401
    async def get_repository(self, repo: str) -> dict[str, Any]:
        owner, name = repo.split("/", 1)
        gh = await self._for_repo(repo)
        response = await gh.rest.repos.async_get(owner, name)
        return response.parsed_data.model_dump()

    @_retry_on_401
    async def get_issue(self, repo: str, issue_number: int) -> dict[str, Any]:
        owner, name = repo.split("/", 1)
        gh = await self._for_repo(repo)
        response = await gh.rest.issues.async_get(owner, name, issue_number)
        return response.parsed_data.model_dump()

    @_retry_on_401
    async def get_pull_request(self, repo: str, pr_number: int) -> dict[str, Any]:
        owner, name = repo.split("/", 1)
        gh = await self._for_repo(repo)
        response = await gh.rest.pulls.async_get(owner, name, pr_number)
        return response.parsed_data.model_dump()

    @_retry_on_401
    async def squash_merge_pull_request(self, repo: str, pr_number: int) -> bool:
        # No commit_title: GitHub then titles the squash commit from the PR title.
        owner, name = repo.split("/", 1)
        gh = await self._for_repo(repo)
        try:
            response = await gh.rest.pulls.async_merge(
                owner,
                name,
                pr_number,
                data={"merge_method": "squash"},
            )
        except RequestFailed as exc:
            if exc.response.status_code in {405, 409}:
                return False
            raise
        return response.parsed_data.merged

    @_retry_on_401
    async def enable_auto_merge(self, repo: str, node_id: str) -> bool:
        gh = await self._for_repo(repo)
        try:
            await gh.async_graphql(
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
        gh = await self._for_repo(repo)
        try:
            await gh.rest.pulls.async_update_branch(owner, name, pr_number)
        except RequestFailed as exc:
            if exc.response.status_code == 422:
                return
            raise

    async def merge_when_ready(self, repo: str, pr_number: int) -> bool:
        """Whether GitHub accepted ownership of the merge."""
        pull_request = await self.get_pull_request(repo, pr_number)
        if pull_request["state"] == "closed":
            return True

        await self.update_pull_request_branch(repo, pr_number)
        if await self.enable_auto_merge(repo, pull_request["node_id"]):
            return True
        return await self.squash_merge_pull_request(repo, pr_number)

    @_retry_on_401
    async def update_pull_request_body(
        self,
        repo: str,
        pr_number: int,
        body: str,
    ) -> dict[str, Any]:
        owner, name = repo.split("/", 1)
        gh = await self._for_repo(repo)
        response = await gh.rest.pulls.async_update(owner, name, pr_number, body=body)
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
        gh = await self._for_repo(repo)
        if draft:
            await gh.async_graphql(
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

        await gh.async_graphql(
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
        gh = await self._for_repo(repo)
        try:
            await gh.rest.git.async_delete_ref(owner, name, f"heads/{branch}")
        except RequestFailed as exc:
            if exc.response.status_code == 404:
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
        gh = await self._for_repo(repo)
        await gh.rest.pulls.async_request_reviewers(
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
        gh = await self._for_repo(repo)
        raw_comments = _build_review_comments(comments) if comments else None
        try:
            kwargs: dict[str, Any] = {
                "event": event,
                "body": body,
            }
            if raw_comments is not None:
                kwargs["comments"] = raw_comments
            response = await gh.rest.pulls.async_create_review(
                owner,
                name,
                pr_number,
                **kwargs,
            )
            return response.parsed_data.model_dump()
        except RequestFailed as exc:
            if exc.response.status_code != 422 or not raw_comments:
                raise
            logger.warning(
                "Review with inline comments rejected (422) for %s#%d; retrying body-only.",
                repo,
                pr_number,
            )
            fallback_body = _fold_comments_into_body(body, comments or [])
            response = await gh.rest.pulls.async_create_review(
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
            gh = await self._for_repo(repo)
            response = await gh.rest.repos.async_get_content(
                owner,
                name,
                path,
                ref=ref or "",
            )
        except GitHubAppNotInstalledError:
            return
        except RequestFailed as exc:
            if exc.response.status_code == 404:
                return
            raise
        data = response.parsed_data
        content = getattr(data, "content", None) or ""
        return base64.b64decode(content).decode()


async def download_public_tarball(owner: str, repo: str, ref: str = "") -> bytes:
    """Gzipped tarball of a *public* repo at ``ref`` (default branch when empty).
    Fetched anonymously — no App installation, unlike ``GitHubClient`` whose every
    call resolves an installation token and so can't reach repos druks isn't on."""
    async with GitHub() as gh:
        response = await gh.rest.repos.async_download_tarball_archive(owner, repo, ref)
        return response.content


def _build_review_comments(comments: list[ReviewComment]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for c in comments:
        entry: dict[str, Any] = {"path": c.path, "line": c.line, "body": c.body}
        if c.start_line is not None:
            entry["start_line"] = c.start_line
        result.append(entry)
    return result


def _fold_comments_into_body(body: str, comments: list[ReviewComment]) -> str:
    lines = [body, "", "---", ""]
    for c in comments:
        loc = f"`{c.path}:{c.line}`"
        if c.start_line is not None:
            loc = f"`{c.path}:{c.start_line}-{c.line}`"
        lines.append(f"**{loc}**\n{c.body}\n")
    return "\n".join(lines)


def get_github_client() -> GitHubClient:
    """The operator client, resolved from the GitHub service-identity row —
    the only credential source; there is no settings or file fallback. Raises
    ``ServiceNotConnectedError`` when GitHub isn't connected. ``github_api_url``
    stays a Settings input because it is transport, not identity. PEM plaintext
    exists only here, feeding the client's auth strategy."""
    row = ServiceIdentity.get(GITHUB)
    return GitHubClient(
        app_id=row.identity["app_id"],
        private_key=row.secrets["private_key"],
        base_url=load_settings().github_api_url,
        slug=row.identity["slug"],
    )
