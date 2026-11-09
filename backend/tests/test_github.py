import os
import subprocess
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, call

import druks.core.apis.github as github_api
import pytest
from conftest import connect_service
from druks.core.apis.exceptions import GitHubAppNotActiveError, GitHubAppNotInstalledError
from druks.core.apis.github import GitHubAppClient, GitHubClient
from druks.core.services import Github
from druks.services import Connection, ServiceNotConnectedError
from githubkit.exception import GraphQLFailed, RequestFailed, RequestTimeout
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


async def test_get_bot_user_id_asks_with_an_active_installation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    users = SimpleNamespace(
        async_get_by_username=AsyncMock(
            return_value=SimpleNamespace(parsed_data=SimpleNamespace(id=123456789))
        )
    )
    tokens = []

    @asynccontextmanager
    async def github(auth, *, base_url, **_):
        tokens.append(auth.token)
        yield SimpleNamespace(rest=SimpleNamespace(users=users))

    monkeypatch.setattr(github_api, "GitHub", github)
    installations = [
        SimpleNamespace(id=1, suspended_at="2026-11-01T00:00:00Z"),
        SimpleNamespace(id=2, suspended_at=None),
    ]
    apps = SimpleNamespace(
        async_list_installations=AsyncMock(return_value=SimpleNamespace(parsed_data=installations))
    )
    client = _TestGitHubAppClient(apps)
    client.create_installation_token = AsyncMock(return_value=("ghs_two", None))

    assert await client.get_bot_user_id() == 123456789
    # A suspended installation mints no token.
    client.create_installation_token.assert_awaited_once_with(2)
    assert tokens == ["ghs_two"]
    users.async_get_by_username.assert_awaited_once_with("example-app[bot]")


async def test_get_bot_user_id_without_an_active_installation_says_so() -> None:
    installations = [SimpleNamespace(id=1, suspended_at="2026-11-01T00:00:00Z")]
    apps = SimpleNamespace(
        async_list_installations=AsyncMock(return_value=SimpleNamespace(parsed_data=installations))
    )

    with pytest.raises(GitHubAppNotActiveError, match="no active installation"):
        await _TestGitHubAppClient(apps).get_bot_user_id()


async def test_the_app_row_keeps_the_bot_user_id(druks_db, monkeypatch: pytest.MonkeyPatch):
    row = await connect_service(
        "github", identity={"app_id": "1", "slug": "druks-acme"}, secrets={"private_key": "pem"}
    )
    app = SimpleNamespace(get_bot_user_id=AsyncMock(return_value=7))
    monkeypatch.setattr(Github, "_app_client", staticmethod(lambda row: app))

    author = await Github.get_bot_git_author()

    assert author == ("druks-acme[bot]", "7+druks-acme[bot]@users.noreply.github.com")
    assert row.identity["bot_user_id"] == 7
    assert await Github.get_bot_git_author() == author
    app.get_bot_user_id.assert_awaited_once()


async def test_github_prepares_a_box_with_the_bot_and_the_github_user(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    env = {**os.environ, "HOME": str(tmp_path), "GIT_CONFIG_GLOBAL": str(tmp_path / ".gitconfig")}

    async def exec(command, *, timeout):
        result = subprocess.run(command, env=env, check=False, capture_output=True, text=True)
        return SimpleNamespace(returncode=result.returncode, stderr=result.stderr)

    monkeypatch.setattr(
        Github,
        "get_bot_git_author",
        AsyncMock(return_value=("app[bot]", "1+app[bot]@users.noreply.github.com")),
    )
    sign_in = SimpleNamespace(
        identity={"authority": "https://github.com", "subject": "42", "login": "octocat"}
    )

    await Github.prepare_sandbox(
        SimpleNamespace(id="sandbox-1", exec=exec),  # type: ignore[arg-type]
        Connection(Github, sign_in),  # type: ignore[arg-type]
    )
    # The agent clones its repos after Druks writes the identity.
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], env=env, check=True)
    hook = repo / ".git" / "hooks" / "prepare-commit-msg"
    message = tmp_path / "COMMIT_EDITMSG"
    message.write_text("Change\n")
    subprocess.run([str(hook), str(message)], cwd=repo, env=env, check=True)
    email = subprocess.run(
        ["git", "-C", str(repo), "config", "user.email"],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert email.stdout.strip() == "1+app[bot]@users.noreply.github.com"
    assert "Co-Authored-By: octocat <42+octocat@users.noreply.github.com>" in message.read_text()
    assert (repo / ".git" / "info" / "exclude").exists()


@pytest.mark.parametrize(
    "error",
    [
        ServiceNotConnectedError("github"),
        GitHubAppNotActiveError("druks-acme"),
        TimeoutError(),
        RequestTimeout(Mock()),
    ],
)
async def test_a_failed_bot_lookup_leaves_the_box_without_an_author(
    monkeypatch: pytest.MonkeyPatch, caplog, error
):
    monkeypatch.setattr(Github, "get_bot_git_author", AsyncMock(side_effect=error))
    host = SimpleNamespace(id="sandbox-1", exec=AsyncMock())
    sign_in = SimpleNamespace(identity={"subject": "42", "login": "octocat"})

    await Github.prepare_sandbox(host, Connection(Github, sign_in))  # type: ignore[arg-type]

    host.exec.assert_not_awaited()
    assert "Sandbox sandbox-1 has no bot author" in caplog.text


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
