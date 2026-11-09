from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, call

import druks.core.apis.github as github_api
import pytest
from druks.core.apis.exceptions import GitHubAppNotInstalledError
from druks.core.apis.github import GitHubAppClient, GitHubClient
from githubkit.exception import GraphQLFailed, RequestFailed
from githubkit.retry import RETRY_SERVER_ERROR


class _Parsed:
    def model_dump(self) -> dict[str, str]:
        return {"id": "review-1"}


class _Response:
    parsed_data = _Parsed()


class _Pulls:
    def __init__(self) -> None:
        self.create_review_kwargs: dict[str, Any] | None = None

    async def async_create_review(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        **kwargs: Any,
    ) -> _Response:
        self.create_review_kwargs = {
            "owner": owner,
            "repo": repo,
            "pr_number": pr_number,
            **kwargs,
        }
        return _Response()


class _Rest:
    def __init__(self) -> None:
        self.pulls = _Pulls()


class _GitHub:
    def __init__(self) -> None:
        self.rest = _Rest()


class _TestGitHubClient(GitHubClient):
    def __init__(self, github: _GitHub) -> None:
        self._github = github


async def test_create_review_omits_empty_comments() -> None:
    github = _GitHub()
    client = _TestGitHubClient(github)

    await client.create_review(
        "ClawHaven/example",
        7,
        event="APPROVE",
        body="Approved.",
    )

    assert github.rest.pulls.create_review_kwargs == {
        "owner": "ClawHaven",
        "repo": "example",
        "pr_number": 7,
        "event": "APPROVE",
        "body": "Approved.",
    }


async def test_enable_auto_merge_uses_squash_mutation() -> None:
    github = SimpleNamespace(async_graphql=AsyncMock())
    client = _TestGitHubClient(github)

    assert await client.enable_auto_merge("ClawHaven/example", "PR_node")

    query, variables = github.async_graphql.await_args.args
    assert "enablePullRequestAutoMerge" in query
    assert "mergeMethod: SQUASH" in query
    assert variables == {"id": "PR_node"}


class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


def _make_request_failed(status_code: int) -> RequestFailed:
    exc = RequestFailed.__new__(RequestFailed)
    exc.response = _FakeResponse(status_code)  # type: ignore[assignment]
    return exc


async def test_enable_auto_merge_absorbs_graphql_rejection() -> None:
    error = GraphQLFailed(SimpleNamespace(errors=["rejected"]))  # type: ignore[arg-type]
    client = _TestGitHubClient(SimpleNamespace(async_graphql=AsyncMock(side_effect=error)))

    assert not await client.enable_auto_merge("ClawHaven/example", "PR_node")


async def test_enable_auto_merge_raises_transport_failure() -> None:
    error = _make_request_failed(500)
    client = _TestGitHubClient(SimpleNamespace(async_graphql=AsyncMock(side_effect=error)))

    with pytest.raises(RequestFailed) as raised:
        await client.enable_auto_merge("ClawHaven/example", "PR_node")

    assert raised.value is error


@pytest.mark.parametrize("status_code", [405, 409])
async def test_squash_merge_pull_request_absorbs_not_mergeable_status(
    status_code: int,
) -> None:
    pulls = SimpleNamespace(async_merge=AsyncMock(side_effect=_make_request_failed(status_code)))
    client = _TestGitHubClient(SimpleNamespace(rest=SimpleNamespace(pulls=pulls)))

    assert not await client.squash_merge_pull_request("ClawHaven/example", 7)


async def test_squash_merge_pull_request_raises_other_failures() -> None:
    error = _make_request_failed(422)
    pulls = SimpleNamespace(async_merge=AsyncMock(side_effect=error))
    client = _TestGitHubClient(SimpleNamespace(rest=SimpleNamespace(pulls=pulls)))

    with pytest.raises(RequestFailed) as raised:
        await client.squash_merge_pull_request("ClawHaven/example", 7)

    assert raised.value is error


async def test_update_pull_request_branch_accepts_already_current_branch() -> None:
    pulls = SimpleNamespace(
        async_update_branch=AsyncMock(side_effect=_make_request_failed(422)),
    )
    client = _TestGitHubClient(SimpleNamespace(rest=SimpleNamespace(pulls=pulls)))

    await client.update_pull_request_branch("ClawHaven/example", 7)

    pulls.async_update_branch.assert_awaited_once_with("ClawHaven", "example", 7)


async def test_update_pull_request_branch_raises_other_failures() -> None:
    error = _make_request_failed(409)
    pulls = SimpleNamespace(async_update_branch=AsyncMock(side_effect=error))
    client = _TestGitHubClient(SimpleNamespace(rest=SimpleNamespace(pulls=pulls)))

    with pytest.raises(RequestFailed) as raised:
        await client.update_pull_request_branch("ClawHaven/example", 7)

    assert raised.value is error


async def test_create_branch_starts_at_the_head_of_the_default_branch() -> None:
    repository = SimpleNamespace(parsed_data=SimpleNamespace(default_branch="trunk"))
    head = SimpleNamespace(parsed_data=SimpleNamespace(object_=SimpleNamespace(sha="abc123")))
    git = SimpleNamespace(async_get_ref=AsyncMock(return_value=head), async_create_ref=AsyncMock())
    repos = SimpleNamespace(async_get=AsyncMock(return_value=repository))
    client = _TestGitHubClient(SimpleNamespace(rest=SimpleNamespace(repos=repos, git=git)))

    await client.create_branch("ClawHaven/example", "agent/ACME-5")

    git.async_get_ref.assert_awaited_once_with("ClawHaven", "example", "heads/trunk")
    git.async_create_ref.assert_awaited_once_with(
        "ClawHaven", "example", ref="refs/heads/agent/ACME-5", sha="abc123"
    )


def _merge_when_ready_client(
    *,
    state: str = "open",
    auto_merge_accepted: bool = True,
    squash_merge_accepted: bool = True,
) -> tuple[_TestGitHubClient, Mock]:
    client = _TestGitHubClient(SimpleNamespace())
    operations = Mock()
    get_pull_request = AsyncMock(return_value={"state": state, "node_id": "PR_node"})
    update_pull_request_branch = AsyncMock()
    enable_auto_merge = AsyncMock(return_value=auto_merge_accepted)
    squash_merge_pull_request = AsyncMock(return_value=squash_merge_accepted)
    operations.attach_mock(get_pull_request, "get_pull_request")
    operations.attach_mock(update_pull_request_branch, "update_pull_request_branch")
    operations.attach_mock(enable_auto_merge, "enable_auto_merge")
    operations.attach_mock(squash_merge_pull_request, "squash_merge_pull_request")
    client.get_pull_request = get_pull_request
    client.update_pull_request_branch = update_pull_request_branch
    client.enable_auto_merge = enable_auto_merge
    client.squash_merge_pull_request = squash_merge_pull_request
    return client, operations


async def test_merge_when_ready_short_circuits_closed_pull_request() -> None:
    client, operations = _merge_when_ready_client(state="closed")

    assert await client.merge_when_ready("ClawHaven/example", 7)
    assert operations.mock_calls == [call.get_pull_request("ClawHaven/example", 7)]


async def test_merge_when_ready_merges_clean_pull_request_directly() -> None:
    client, operations = _merge_when_ready_client()

    assert await client.merge_when_ready("ClawHaven/example", 7)
    assert operations.mock_calls == [
        call.get_pull_request("ClawHaven/example", 7),
        call.squash_merge_pull_request("ClawHaven/example", 7),
    ]


async def test_merge_when_ready_arms_auto_merge_before_updating_branch() -> None:
    client, operations = _merge_when_ready_client(squash_merge_accepted=False)

    assert await client.merge_when_ready("ClawHaven/example", 7)
    assert operations.mock_calls == [
        call.get_pull_request("ClawHaven/example", 7),
        call.squash_merge_pull_request("ClawHaven/example", 7),
        call.enable_auto_merge("ClawHaven/example", "PR_node"),
        call.update_pull_request_branch("ClawHaven/example", 7),
    ]


async def test_merge_when_ready_reports_unsettled_merge() -> None:
    client, operations = _merge_when_ready_client(
        auto_merge_accepted=False,
        squash_merge_accepted=False,
    )

    assert not await client.merge_when_ready("ClawHaven/example", 7)
    assert operations.mock_calls == [
        call.get_pull_request("ClawHaven/example", 7),
        call.squash_merge_pull_request("ClawHaven/example", 7),
        call.enable_auto_merge("ClawHaven/example", "PR_node"),
    ]


class _TestGitHubAppClient(GitHubAppClient):
    def __init__(self, apps: Any) -> None:
        self._slug = "example-app"
        self._app = SimpleNamespace(rest=SimpleNamespace(apps=apps))


async def test_an_account_installation_falls_back_to_the_user_endpoint() -> None:
    apps = SimpleNamespace(
        async_get_org_installation=AsyncMock(side_effect=_make_request_failed(404)),
        async_get_user_installation=AsyncMock(
            return_value=SimpleNamespace(parsed_data=SimpleNamespace(id=42))
        ),
    )

    assert await _TestGitHubAppClient(apps).get_installation_id("czpython") == 42
    apps.async_get_org_installation.assert_awaited_once_with("czpython")
    apps.async_get_user_installation.assert_awaited_once_with("czpython")


async def test_an_org_installation_does_not_consult_the_user_endpoint() -> None:
    apps = SimpleNamespace(
        async_get_org_installation=AsyncMock(
            return_value=SimpleNamespace(parsed_data=SimpleNamespace(id=41))
        ),
        async_get_user_installation=AsyncMock(),
    )

    assert await _TestGitHubAppClient(apps).get_installation_id("clawhaven") == 41
    apps.async_get_user_installation.assert_not_awaited()


async def test_installation_lookup_404_names_the_repo() -> None:
    """An App without access to the repo fails at the installation lookup. The raw
    githubkit repr (``Response(404 Not Found, …)``) is what a run's failure column
    would show the operator — translate it into an error that names the repo and
    the fix."""
    apps = SimpleNamespace(
        async_get_repo_installation=AsyncMock(side_effect=_make_request_failed(404))
    )

    with pytest.raises(GitHubAppNotInstalledError) as raised:
        await _TestGitHubAppClient(apps).get_installation_id("ClawHaven/acme-app")

    assert "The GitHub App example-app has no access to ClawHaven/acme-app" in str(raised.value)
    assert "install the app" in str(raised.value)


async def test_list_repos_paginates() -> None:
    first_page = [
        SimpleNamespace(full_name=f"czpython/repo-{number}", description=None)
        for number in range(100)
    ]
    second_page = [
        SimpleNamespace(full_name="czpython/repo-100", description="first"),
        SimpleNamespace(full_name="czpython/repo-101", description="second"),
    ]
    apps = SimpleNamespace(
        async_list_repos_accessible_to_installation=AsyncMock(
            side_effect=[
                SimpleNamespace(parsed_data=SimpleNamespace(repositories=first_page)),
                SimpleNamespace(parsed_data=SimpleNamespace(repositories=second_page)),
            ]
        )
    )
    client = _TestGitHubClient(SimpleNamespace(rest=SimpleNamespace(apps=apps)))

    repos = await client.list_repos()

    assert len(repos) == 102
    assert repos[0] == {"full_name": "czpython/repo-0", "description": None}
    assert repos[-1] == {"full_name": "czpython/repo-101", "description": "second"}
    apps.async_list_repos_accessible_to_installation.assert_has_awaits(
        [call(per_page=100, page=1), call(per_page=100, page=2)]
    )


async def test_get_bot_git_author_looks_up_the_bot_once(monkeypatch: pytest.MonkeyPatch) -> None:
    # The author is the App's bot user with GitHub's <id>+<login> noreply
    # address; the id comes from one lookup, cached for the life of the process.
    monkeypatch.setattr(github_api, "_BOT_USER_ID_CACHE", {})
    users = SimpleNamespace(
        async_get_by_username=AsyncMock(
            return_value=SimpleNamespace(parsed_data=SimpleNamespace(id=123456789))
        )
    )
    client = _TestGitHubClient(SimpleNamespace(rest=SimpleNamespace(users=users)))
    client._app_id, client._slug = "12345", "example-app"

    author = await client.get_bot_git_author()

    assert author == (
        "example-app[bot]",
        "123456789+example-app[bot]@users.noreply.github.com",
    )
    assert await client.get_bot_git_author() == author
    users.async_get_by_username.assert_awaited_once_with("example-app[bot]")


def _template_generating_client(repo_apis: Any) -> _TestGitHubClient:
    return _TestGitHubClient(SimpleNamespace(rest=SimpleNamespace(repos=repo_apis)))


async def test_create_repo_from_template_generates_a_private_repo() -> None:
    repo_apis = SimpleNamespace(async_create_using_template=AsyncMock())
    client = _template_generating_client(repo_apis)

    full_name = await client.create_repo_from_template(
        "clawhaven", "clawhaven/shop-template", "acme-coffee"
    )

    assert full_name == "clawhaven/acme-coffee"
    repo_apis.async_create_using_template.assert_awaited_once_with(
        "clawhaven",
        "shop-template",
        name="acme-coffee",
        owner="clawhaven",
        private=True,
    )


async def test_create_repo_from_template_reads_a_taken_name_as_already_created() -> None:
    repo_apis = SimpleNamespace(
        async_create_using_template=AsyncMock(side_effect=_make_request_failed(422))
    )
    client = _template_generating_client(repo_apis)

    full_name = await client.create_repo_from_template(
        "clawhaven", "clawhaven/shop-template", "acme-coffee"
    )

    assert full_name == "clawhaven/acme-coffee"


class _Checks:
    def __init__(self, posted_ids: tuple[int, ...] = ()) -> None:
        self.posted_ids = posted_ids
        self.calls: list[tuple[str, Any]] = []

    async def async_list_for_ref(self, owner: str, repo: str, ref: str, **kwargs: Any) -> Any:
        runs = [SimpleNamespace(id=run_id) for run_id in self.posted_ids]
        return SimpleNamespace(parsed_data=SimpleNamespace(check_runs=runs))

    async def async_create(self, owner: str, repo: str, *, data: dict[str, Any]) -> Any:
        self.calls.append(("create", data))
        return SimpleNamespace(parsed_data=SimpleNamespace(id=900))

    async def async_update(
        self, owner: str, repo: str, check_run_id: int, *, data: dict[str, Any]
    ) -> Any:
        self.calls.append(("update", {"check_run_id": check_run_id, **data}))


def _checks_client(checks: _Checks) -> _TestGitHubClient:
    client = _TestGitHubClient(SimpleNamespace(rest=SimpleNamespace(checks=checks)))
    client._app_id = "41"
    return client


async def test_post_check_run_creates_the_run_and_sends_annotations_in_batches() -> None:
    checks = _Checks()

    await _checks_client(checks).post_check_run(
        "acme/widget",
        head_sha="0f3a2c1d",
        name="druks-house-rules",
        status="completed",
        conclusion="failure",
        title="120 findings",
        summary="One annotation per finding.",
        annotations=[{"path": "app.py", "start_line": line} for line in range(120)],
    )

    assert [name for name, _ in checks.calls] == ["create", "update", "update"]
    assert checks.calls[0][1]["head_sha"] == "0f3a2c1d"
    assert checks.calls[0][1]["conclusion"] == "failure"
    assert [len(data["output"]["annotations"]) for _, data in checks.calls] == [50, 50, 20]
    assert {data["check_run_id"] for _, data in checks.calls[1:]} == {900}


async def test_post_check_run_updates_the_run_this_app_already_posted() -> None:
    checks = _Checks(posted_ids=(77,))

    await _checks_client(checks).post_check_run(
        "acme/widget",
        head_sha="0f3a2c1d",
        name="druks-house-rules",
        status="in_progress",
        title="Checking",
        summary="",
    )

    assert checks.calls == [
        (
            "update",
            {
                "check_run_id": 77,
                "name": "druks-house-rules",
                "status": "in_progress",
                "output": {"title": "Checking", "summary": "", "annotations": []},
            },
        )
    ]


def test_github_clients_wait_with_a_limit() -> None:
    clients = [
        GitHubClient("token", app_id="1", slug="druks")._github,
        GitHubAppClient(app_id="1", private_key="key")._app,
    ]

    assert [client.config.timeout for client in clients] == [github_api.DEFAULT_TIMEOUT] * 2
    assert [client.config.auto_retry for client in clients] == [RETRY_SERVER_ERROR] * 2
