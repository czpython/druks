import base64
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import Any, Literal

import httpx
from githubkit import AppAuthStrategy, GitHub, TokenAuthStrategy
from githubkit.exception import GraphQLFailed, RequestFailed
from githubkit.retry import RETRY_SERVER_ERROR
from githubkit.utils import UNSET

from druks.core.apis.exceptions import GitHubAppNotActiveError, GitHubAppNotInstalledError
from druks.core.utils.time import ensure_utc

logger = logging.getLogger(__name__)

GITHUB_API_URL = "https://api.github.com"
# The authority of a GitHub sign-in: one GitHub, so a user id names one person.
GITHUB_AUTHORITY = "https://github.com"

# githubkit waits with no limit unless it gets a timeout. The read limit applies to
# each read, not to the whole response, so a large tarball still downloads.
DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=5.0, write=10.0, pool=5.0)


@dataclass(frozen=True)
class ReviewComment:
    path: str
    line: int
    body: str
    start_line: int | None = None


_ANNOTATIONS_PER_REQUEST = 50


# Where-druks-may-act is derived from the App's installations. Module-level
# so it survives the per-call-site client construction; keyed by app id;
# serves the last-known set when GitHub hiccups.
_INSTALLATION_ACCOUNTS_TTL_SECONDS = 600.0
_INSTALLATION_ACCOUNTS_CACHE: dict[str, tuple[float, tuple[str, ...]]] = {}


class GitHubAppClient:
    """The App's key signs its calls at ``app_url``: the App itself, its installations,
    and a token for one of them."""

    def __init__(
        self,
        *,
        app_id: str,
        private_key: str,
        app_url: str = GITHUB_API_URL,
        slug: str = "",
    ) -> None:
        self._app_id = app_id
        self._slug = slug
        self._app = GitHub(
            AppAuthStrategy(app_id, private_key),
            base_url=app_url,
            timeout=DEFAULT_TIMEOUT,
            auto_retry=RETRY_SERVER_ERROR,
        )

    async def get_installation(self, owner_or_repo: str):
        """The installation that covers a repository ``owner/name``, or an account."""
        owner, _, name = owner_or_repo.partition("/")
        apps = self._app.rest.apps
        if name:
            lookups = [partial(apps.async_get_repo_installation, owner, name)]
        else:
            # An organization and a user each have their own endpoint.
            lookups = [
                partial(apps.async_get_org_installation, owner),
                partial(apps.async_get_user_installation, owner),
            ]
        for lookup in lookups:
            try:
                return (await lookup()).parsed_data
            except RequestFailed as error:
                if error.response.status_code != 404:
                    raise
        raise GitHubAppNotInstalledError(owner_or_repo, app=self._slug)

    async def create_installation_token(
        self, installation_id: int, repositories: Sequence[str] = ()
    ) -> tuple[str, datetime]:
        """A token for the installation, and the expiry GitHub gave it. Named
        ``repositories`` limit it to them; without them it covers the whole installation."""
        response = await self._app.rest.apps.async_create_installation_access_token(
            installation_id, repositories=list(repositories) or UNSET
        )
        minted = response.parsed_data
        return str(minted.token), ensure_utc(datetime.fromisoformat(str(minted.expires_at)))

    async def get_bot_user_id(self) -> int:
        """The user id of the App's bot user, ``<slug>[bot]``. The users endpoint
        answers a token of the first installation that is not suspended: GitHub
        limits anonymous requests to 60 an hour per IP."""
        response = await self._app.rest.apps.async_list_installations(per_page=100)
        installation_ids = [item.id for item in response.parsed_data if not item.suspended_at]
        if not installation_ids:
            raise GitHubAppNotActiveError(self._slug)
        token, _ = await self.create_installation_token(installation_ids[0])
        async with GitHub(
            TokenAuthStrategy(token),
            base_url=GITHUB_API_URL,
            timeout=DEFAULT_TIMEOUT,
            auto_retry=RETRY_SERVER_ERROR,
        ) as client:
            response = await client.rest.users.async_get_by_username(f"{self._slug}[bot]")
        return response.parsed_data.id

    async def list_installation_accounts(self, cached: bool = True) -> tuple[str, ...]:
        """Account logins (orgs/users) this App is installed on — the
        authoritative "where druks may act". Install the App somewhere and
        druks works there; uninstall and it stops. Cached ~10 min per app
        id, serving the last-known set when GitHub hiccups. ``cached=False``
        asks now, after an install."""
        known = _INSTALLATION_ACCOUNTS_CACHE.get(self._app_id)
        now = time.monotonic()
        if cached and known and now - known[0] < _INSTALLATION_ACCOUNTS_TTL_SECONDS:
            return known[1]
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
            if known:
                logger.warning(
                    "Could not refresh App installations; serving the last-known set.",
                    exc_info=True,
                )
                return known[1]
            raise
        result = tuple(dict.fromkeys(accounts))
        _INSTALLATION_ACCOUNTS_CACHE[self._app_id] = (now, result)
        return result

    async def get_app_permissions(self) -> dict[str, str]:
        """What GitHub grants the App today, by permission name."""
        response = await self._app.rest.apps.async_get_authenticated()
        return response.json()["permissions"]

    async def get_authenticated_app_slug(self) -> str:
        """The slug GitHub reports for these credentials — a live App-JWT call,
        so it proves the App ID matches the PEM. The connect flow's validation;
        mention resolution reads the stored slug instead."""
        response = await self._app.rest.apps.async_get_authenticated()
        return str(getattr(response.parsed_data, "slug", None) or "")


class GitHubClient:
    """The App acting in one installation, with the token minted for it."""

    def __init__(self, token: str, *, app_id: str, slug: str) -> None:
        self._app_id = app_id
        self._slug = slug
        self._github = GitHub(
            TokenAuthStrategy(token),
            base_url=GITHUB_API_URL,
            timeout=DEFAULT_TIMEOUT,
            auto_retry=RETRY_SERVER_ERROR,
        )

    async def list_repos(self) -> list[dict[str, Any]]:
        """The repositories the installation can see, as
        ``[{"full_name": "<owner>/<name>", "description": ...}, ...]``."""
        repos: list[dict[str, Any]] = []
        page = 1
        while True:
            response = await self._github.rest.apps.async_list_repos_accessible_to_installation(
                per_page=100,
                page=page,
            )
            items = response.parsed_data.repositories
            repos.extend(
                {"full_name": repository.full_name, "description": repository.description}
                for repository in items
            )
            if len(items) < 100:
                break
            page += 1
        return repos

    async def create_repo_from_template(self, owner: str, template_repo: str, name: str) -> str:
        """A new private repo ``name`` under ``owner``, generated from the
        ``owner/name`` template ``template_repo``; returns its ``full_name``.
        Safe to retry: a name already taken reads as already created."""
        template_owner, template_name = template_repo.split("/", 1)
        full_name = f"{owner}/{name}"
        try:
            await self._github.rest.repos.async_create_using_template(
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

    async def get_repository(self, repo: str) -> dict[str, Any]:
        owner, name = repo.split("/", 1)
        response = await self._github.rest.repos.async_get(owner, name)
        return response.parsed_data.model_dump()

    async def get_issue(self, repo: str, issue_number: int) -> dict[str, Any]:
        owner, name = repo.split("/", 1)
        response = await self._github.rest.issues.async_get(owner, name, issue_number)
        return response.parsed_data.model_dump()

    async def list_comments(self, repo: str, issue_number: int) -> list[dict[str, Any]]:
        """Every top-level comment on the issue or pull request, oldest first."""
        owner, name = repo.split("/", 1)
        comments = self._github.rest.paginate(
            self._github.rest.issues.async_list_comments,
            owner=owner,
            repo=name,
            issue_number=issue_number,
            per_page=100,
        )
        return [comment.model_dump() async for comment in comments]

    async def list_review_comments(self, repo: str, pr_number: int) -> list[dict[str, Any]]:
        """Every inline comment on the pull request, oldest first."""
        owner, name = repo.split("/", 1)
        comments = self._github.rest.paginate(
            self._github.rest.pulls.async_list_review_comments,
            owner=owner,
            repo=name,
            pull_number=pr_number,
            per_page=100,
        )
        return [comment.model_dump() async for comment in comments]

    async def create_comment(self, repo: str, issue_number: int, body: str) -> dict[str, Any]:
        owner, name = repo.split("/", 1)
        response = await self._github.rest.issues.async_create_comment(
            owner, name, issue_number, body=body
        )
        return response.parsed_data.model_dump()

    async def create_reaction(self, repo: str, comment_id: int, content: str) -> None:
        """React to a top-level comment. GitHub keeps one reaction per App and content,
        so reacting again changes nothing."""
        owner, name = repo.split("/", 1)
        await self._github.rest.reactions.async_create_for_issue_comment(
            owner, name, comment_id, content=content
        )

    async def create_review_comment_reaction(
        self, repo: str, comment_id: int, content: str
    ) -> None:
        """React to a comment in a pull request review."""
        owner, name = repo.split("/", 1)
        await self._github.rest.reactions.async_create_for_pull_request_review_comment(
            owner, name, comment_id, content=content
        )

    async def reply_to_review_comment(
        self, repo: str, pr_number: int, comment_id: int, body: str
    ) -> dict[str, Any]:
        """A reply in the inline thread that ``comment_id`` starts. GitHub takes the
        thread's first comment only, never a reply in it."""
        owner, name = repo.split("/", 1)
        response = await self._github.rest.pulls.async_create_reply_for_review_comment(
            owner, name, pr_number, comment_id, body=body
        )
        return response.parsed_data.model_dump()

    async def replace_issue_labels(
        self, repo: str, issue_number: int, *, add: str, remove: set[str]
    ) -> None:
        """Put ``add`` on the issue and take ``remove`` off. GitHub answers 404 for a
        label the issue does not carry, and that label is already off."""
        owner, name = repo.split("/", 1)
        await self._github.rest.issues.async_add_labels(owner, name, issue_number, data=[add])
        for label in remove:
            try:
                await self._github.rest.issues.async_remove_label(owner, name, issue_number, label)
            except RequestFailed as error:
                if error.response.status_code != 404:
                    raise

    async def close_issue(self, repo: str, issue_number: int) -> None:
        owner, name = repo.split("/", 1)
        await self._github.rest.issues.async_update(
            owner, name, issue_number, state="closed", state_reason="completed"
        )

    async def get_pull_request(self, repo: str, pr_number: int) -> dict[str, Any]:
        owner, name = repo.split("/", 1)
        response = await self._github.rest.pulls.async_get(owner, name, pr_number)
        return response.parsed_data.model_dump()

    async def squash_merge_pull_request(self, repo: str, pr_number: int) -> bool:
        # No commit_title: GitHub then titles the squash commit from the PR title.
        owner, name = repo.split("/", 1)
        try:
            response = await self._github.rest.pulls.async_merge(
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

    async def enable_auto_merge(self, repo: str, node_id: str) -> bool:
        try:
            await self._github.async_graphql(
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

    async def update_pull_request_branch(self, repo: str, pr_number: int) -> None:
        owner, name = repo.split("/", 1)
        try:
            await self._github.rest.pulls.async_update_branch(owner, name, pr_number)
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

    async def update_pull_request_body(
        self,
        repo: str,
        pr_number: int,
        body: str,
    ) -> dict[str, Any]:
        owner, name = repo.split("/", 1)
        response = await self._github.rest.pulls.async_update(owner, name, pr_number, body=body)
        return response.parsed_data.model_dump()

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
        if draft:
            await self._github.async_graphql(
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

        await self._github.async_graphql(
            """
            mutation MarkPullRequestReadyForReview($id: ID!) {
              markPullRequestReadyForReview(input: {pullRequestId: $id}) {
                pullRequest { id }
              }
            }
            """,
            {"id": node_id},
        )

    async def has_branch(self, repo: str, branch: str) -> bool:
        owner, name = repo.split("/", 1)
        try:
            await self._github.rest.git.async_get_ref(owner, name, f"heads/{branch}")
        except RequestFailed as error:
            if error.response.status_code == 404:
                return False
            raise

        return True

    async def create_branch(self, repo: str, branch: str) -> None:
        """Create ``branch`` at the head of the default branch."""
        owner, name = repo.split("/", 1)
        repository = await self._github.rest.repos.async_get(owner, name)
        head = await self._github.rest.git.async_get_ref(
            owner, name, f"heads/{repository.parsed_data.default_branch}"
        )
        await self._github.rest.git.async_create_ref(
            owner, name, ref=f"refs/heads/{branch}", sha=head.parsed_data.object_.sha
        )

    async def delete_branch(self, repo: str, branch: str) -> bool:
        owner, name = repo.split("/", 1)
        try:
            await self._github.rest.git.async_delete_ref(owner, name, f"heads/{branch}")
        except RequestFailed as error:
            if error.response.status_code == 404:
                return False
            raise

        return True

    async def request_pull_request_reviewers(
        self,
        repo: str,
        pr_number: int,
        reviewers: list[str],
    ) -> None:
        owner, name = repo.split("/", 1)
        await self._github.rest.pulls.async_request_reviewers(
            owner,
            name,
            pr_number,
            reviewers=reviewers,
        )

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
        raw_comments = _build_review_comments(comments) if comments else None
        try:
            kwargs: dict[str, Any] = {
                "event": event,
                "body": body,
            }
            if raw_comments:
                kwargs["comments"] = raw_comments
            response = await self._github.rest.pulls.async_create_review(
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
            response = await self._github.rest.pulls.async_create_review(
                owner,
                name,
                pr_number,
                event=event,
                body=fallback_body,  # type: ignore[arg-type]
            )
            return response.parsed_data.model_dump()

    async def post_check_run(
        self,
        repo: str,
        *,
        head_sha: str,
        name: str,
        status: str,
        conclusion: str = "",
        title: str,
        summary: str,
        annotations: Sequence[dict[str, Any]] = (),
    ) -> None:
        """Create the check run ``name`` on ``head_sha``, or update the one this App
        already posted there. An annotation is GitHub's shape: ``path``, ``start_line``,
        ``end_line``, ``annotation_level``, ``message``."""
        owner, repo_name = repo.split("/", 1)
        checks = self._github.rest.checks
        posted = await checks.async_list_for_ref(
            owner, repo_name, head_sha, check_name=name, app_id=int(self._app_id)
        )
        check_run_id = next((run.id for run in posted.parsed_data.check_runs), None)
        limit = _ANNOTATIONS_PER_REQUEST
        output = {"title": title, "summary": summary}
        body: dict[str, Any] = {
            "name": name,
            "status": status,
            "output": {**output, "annotations": list(annotations[:limit])},
        }
        if conclusion:
            body["conclusion"] = conclusion
        if check_run_id:
            await checks.async_update(owner, repo_name, check_run_id, data=body)
        else:
            response = await checks.async_create(
                owner, repo_name, data={**body, "head_sha": head_sha}
            )
            check_run_id = response.parsed_data.id
        for start in range(limit, len(annotations), limit):
            body["output"] = {**output, "annotations": list(annotations[start : start + limit])}
            await checks.async_update(owner, repo_name, check_run_id, data=body)

    async def get_file_content(
        self,
        repo: str,
        path: str,
        *,
        ref: str | None = None,
    ) -> str | None:
        owner, name = repo.split("/", 1)
        try:
            response = await self._github.rest.repos.async_get_content(
                owner,
                name,
                path,
                ref=ref or "",
            )
        except RequestFailed as error:
            if error.response.status_code == 404:
                return
            raise
        data = response.parsed_data
        content = getattr(data, "content", None) or ""
        return base64.b64decode(content).decode()

    async def download_tarball(self, repo: str) -> bytes:
        """Gzipped tarball of ``repo``'s default branch."""
        owner, name = repo.split("/", 1)
        response = await self._github.rest.repos.async_download_tarball_archive(owner, name, "")
        return response.content


async def download_public_tarball(repo: str) -> bytes:
    """Gzipped tarball of a *public* repo's default branch.
    Fetched anonymously — no App installation, unlike ``GitHubClient``, which acts
    with an installation's token and so reaches only the repos the App is on."""
    owner, name = repo.split("/", 1)
    async with GitHub(timeout=DEFAULT_TIMEOUT, auto_retry=RETRY_SERVER_ERROR) as github:
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
