from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import connect_service
from druks.apps.settings import field_kind, field_multiline
from druks.contrib.software_factory import subscribers  # noqa: F401 — the import registers it
from druks.contrib.software_factory.app import check_review_identity
from druks.contrib.software_factory.datastructures import PullRequest
from druks.contrib.software_factory.github import get_review_actor
from druks.contrib.software_factory.services import GithubReviewer
from druks.contrib.software_factory.workflows import PullRequestReview
from druks.core.services import Github
from druks.prompts import render_prompt
from druks.services.exceptions import ServiceNotConnectedError
from druks.signals import publish
from druks.testing import configure_app_for_test, make_settings, seed_run
from druks.workflows import _bind_instance
from fastapi.testclient import TestClient

pytestmark = pytest.mark.usefixtures("druks_without_remote_config")


@pytest.fixture
def client(tmp_path: Path, druks_db, monkeypatch):
    monkeypatch.setenv("DRUKS_DATA_DIR", str(tmp_path))
    with TestClient(configure_app_for_test(settings=make_settings(tmp_path))) as client:
        yield client


def test_a_pull_requests_identity_is_its_handle():
    # This spelling is the dedup key, the route, the event identity and the replay
    # lookup all at once — normalising or rearranging it splits one review in two.
    pull_request = PullRequest.get("acme/app", 7)

    assert pull_request.identity == {"type": "pull_request", "id": "acme/app#7"}
    assert (pull_request.repo, pull_request.number) == ("acme/app", 7)
    assert pull_request.label == "acme/app#7"


@pytest.mark.parametrize("subject_id", ["acme/app", "acme/app#", "acme/app#0", "app#7", "#7"])
async def test_an_id_that_names_no_pull_request_is_a_miss(subject_id):
    assert await PullRequest.get_for_subject_id(subject_id) is None


async def test_a_pull_request_heads_its_own_page():
    summary = (await PullRequest.get_for_subject_id("acme/app#7")).get_summary()

    assert summary.repo == "acme/app"
    assert summary.pr_number == 7
    assert summary.pull_request_url == "https://github.com/acme/app/pull/7"


async def test_the_pull_request_board_and_page_mount(client: TestClient, druks_db):
    # PullRequestReview declares PullRequest, so the app mounts its board and
    # page — keyed by a handle that carries both a path separator and a `#`.
    pull_request = PullRequest.get("acme/app", 7)
    await seed_run(druks_db, kind=PullRequestReview.kind, subject=pull_request, state="running")

    (row,) = client.get("/api/software_factory/pull_request").json()["rows"]
    assert row["summary"]["id"] == "acme/app#7"
    assert row["status"]["state"] == "running"

    detail = client.get("/api/software_factory/pull_request/acme/app%237").json()
    assert detail["summary"]["pullRequestUrl"] == "https://github.com/acme/app/pull/7"
    assert [entry["kind"] for entry in detail["timeline"]] == [PullRequestReview.kind]
    assert client.get("/api/software_factory/pull_request/acme/app").status_code == 404


def test_the_run_carries_the_pull_request_once():
    # The repo and the number are the subject, so they are not also input: the body
    # takes what only the request knows.
    assert list(PullRequestReview._run_input_model.model_fields) == ["requested_by"]


async def test_a_queued_run_replays_through_its_subject():
    # A review enqueued before the repo and number came off the input still carries
    # them in its durable payload. The extra keys are ignored and the subject rides
    # separately, so the body binds and reads the pull request off the declaration.
    instance, run_kwargs = _bind_instance(
        PullRequestReview,
        PullRequest.get("acme/app", 7).identity,
        {"repo": "acme/app", "pr_number": 7, "requested_by": "dev@example.com"},
        account_id="review-account",
    )

    assert run_kwargs == {"requested_by": "dev@example.com"}
    subject = await instance.subject
    assert (subject.repo, subject.number) == ("acme/app", 7)


async def test_the_reviewer_prompt_names_the_pull_request_it_is_about():
    workflow = SimpleNamespace(
        subject=PullRequest.get("acme/app", 7),
        input=SimpleNamespace(requested_by="dev@example.com"),
    )
    workspace = SimpleNamespace(
        repo_path="/home/agent/work/repo", related_root="/home/agent/related"
    )

    output = await render_prompt(
        "software_factory/review/review_pull_request.md",
        workflow=workflow,
        workspace=workspace,
        siblings=[],
        review_mode="approve",
    )

    assert "pull request #7 on `acme/app`" in output
    assert "at dev@example.com's request" in output
    assert "`COMMENT` event" not in output


async def test_comment_mode_reviews_publish_as_comments():
    # Unset review identity: the operator authors druks's own pull requests, so
    # its reviews publish as comments — the prompt carries that rule.
    workflow = SimpleNamespace(
        subject=PullRequest.get("acme/app", 7),
        input=SimpleNamespace(requested_by="dev@example.com"),
    )
    workspace = SimpleNamespace(
        repo_path="/home/agent/work/repo", related_root="/home/agent/related"
    )

    output = await render_prompt(
        "software_factory/review/review_pull_request.md",
        workflow=workflow,
        workspace=workspace,
        siblings=[],
        review_mode="comment",
    )

    assert "`COMMENT` event" in output


async def _connect_operator() -> None:
    await connect_service(
        "github",
        identity={"app_id": "1", "slug": "druks-operator"},
        secrets={"private_key": "operator-pem", "webhook_secret": "hook-secret"},
    )


async def _connect_reviewer() -> None:
    await connect_service(
        "github_reviewer",
        identity={"app_id": "2", "slug": "druks-reviewer"},
        secrets={"private_key": "review-pem\nline-two"},
    )


async def test_a_connected_reviewer_approves(druks_db):
    await _connect_operator()
    await _connect_reviewer()

    actor = await get_review_actor()

    assert (actor.mode, actor.service) == ("approve", GithubReviewer)
    assert actor.client._app_id == "2"


async def test_an_unconnected_reviewer_borrows_the_operator_in_comment_mode(druks_db):
    await _connect_operator()

    actor = await get_review_actor()

    assert (actor.mode, actor.service) == ("comment", Github)
    assert actor.client._app_id == "1"


def test_the_reviewer_is_an_optional_service_the_app_declares():
    assert (GithubReviewer.slug, GithubReviewer.required) == ("github_reviewer", False)
    assert GithubReviewer.secret_name == "github"
    fields = GithubReviewer.Settings.model_fields
    assert set(fields) == {"app_id", "private_key"}
    assert field_kind(fields["private_key"]) == "secret"
    assert field_multiline(fields["private_key"])
    assert not field_multiline(fields["app_id"])


async def test_review_identity_check_is_healthy_connected_or_not(druks_db):
    assert (await check_review_identity()).ok
    assert "unset" in (await check_review_identity()).detail

    await _connect_reviewer()

    result = await check_review_identity()
    assert result.ok
    assert "reviewer App" in result.detail


async def test_review_dispatch_refuses_before_start_without_github(druks_db, monkeypatch):
    started = []

    async def _start(cls, **kwargs):
        started.append(kwargs)
        return "run-review"

    monkeypatch.setattr(PullRequestReview, "start", classmethod(_start))

    with pytest.raises(ServiceNotConnectedError, match="github is not connected"):
        await PullRequestReview.dispatch(
            repo="acme/app", pr_number=7, requested_by="dev@example.com"
        )

    assert not started


async def test_review_dispatch_starts_once_github_is_connected(druks_db, monkeypatch):
    await _connect_operator()
    started = []

    async def _start(cls, **kwargs):
        started.append(kwargs)
        return "run-review"

    monkeypatch.setattr(PullRequestReview, "start", classmethod(_start))

    run_id = await PullRequestReview.dispatch(
        repo="acme/app", pr_number=7, requested_by="dev@example.com"
    )

    assert run_id == "run-review"
    assert len(started) == 1


async def test_a_passer_by_cannot_spend_a_review(monkeypatch):
    # The repos druks reviews are public, so the mention is the whole internet's to write
    # and each one would spend a run. The filter answers before the body asks GitHub.
    dispatched = []

    async def dispatch(**kwargs):
        dispatched.append(kwargs)

    monkeypatch.setattr(PullRequestReview, "dispatch", dispatch)

    await publish(
        "pr.commented",
        repo="acme/app",
        pr_number=7,
        payload={"author": "passer-by", "author_can_write": False, "body": "@druks review"},
    )

    assert not dispatched
