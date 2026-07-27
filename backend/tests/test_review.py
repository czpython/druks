from pathlib import Path
from types import SimpleNamespace

import pytest
from druks.contrib.review.datastructures import PullRequest
from druks.contrib.review.workflows import PullRequestReview
from druks.prompts import render_prompt
from druks.testing import seed_run
from druks.workflows import _bind_instance
from fastapi.testclient import TestClient

pytestmark = pytest.mark.usefixtures("druks_without_remote_config")


@pytest.fixture
def client(tmp_path: Path, druks_db, monkeypatch):
    from druks.testing import configure_app_for_test, make_settings

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
def test_an_id_that_names_no_pull_request_is_a_miss(subject_id):
    assert PullRequest.get_for_subject_id(subject_id) is None


def test_a_pull_request_heads_its_own_page():
    summary = PullRequest.get_for_subject_id("acme/app#7").get_summary()

    assert summary.repo == "acme/app"
    assert summary.pr_number == 7
    assert summary.pull_request_url == "https://github.com/acme/app/pull/7"


def test_the_pull_request_board_and_page_mount(client: TestClient, druks_db):
    # PullRequestReview declares PullRequest, so the extension mounts its board and
    # page — keyed by a handle that carries both a path separator and a `#`.
    pull_request = PullRequest.get("acme/app", 7)
    seed_run(druks_db, kind=PullRequestReview.kind, subject=pull_request, state="running")

    (row,) = client.get("/api/review/pull_request").json()["rows"]
    assert row["summary"]["id"] == "acme/app#7"
    assert row["status"]["state"] == "running"

    detail = client.get("/api/review/pull_request/acme/app%237").json()
    assert detail["summary"]["pullRequestUrl"] == "https://github.com/acme/app/pull/7"
    assert [entry["kind"] for entry in detail["timeline"]] == [PullRequestReview.kind]
    assert client.get("/api/review/pull_request/acme/app").status_code == 404


def test_the_run_carries_the_pull_request_once():
    # The repo and the number are the subject, so they are not also input: the body
    # takes what only the request knows.
    assert list(PullRequestReview._run_input_model.model_fields) == ["requested_by"]


def test_a_queued_run_replays_through_its_subject():
    # A review enqueued before the repo and number came off the input still carries
    # them in its durable payload. The extra keys are ignored and the subject rides
    # separately, so the body binds and reads the pull request off the declaration.
    instance, run_kwargs = _bind_instance(
        PullRequestReview,
        PullRequest.get("acme/app", 7).identity,
        {"repo": "acme/app", "pr_number": 7, "requested_by": "dev@example.com"},
    )

    assert run_kwargs == {"requested_by": "dev@example.com"}
    assert (instance.subject.repo, instance.subject.number) == ("acme/app", 7)


async def test_the_reviewer_prompt_names_the_pull_request_it_is_about():
    workflow = SimpleNamespace(
        subject=PullRequest.get("acme/app", 7),
        input=SimpleNamespace(requested_by="dev@example.com"),
    )
    workspace = SimpleNamespace(
        repo_path="/home/agent/work/repo", related_root="/home/agent/related"
    )

    output = await render_prompt(
        "review/review_pull_request.md",
        workflow=workflow,
        workspace=workspace,
        siblings=[],
    )

    assert "pull request #7 on `acme/app`" in output
    assert "at dev@example.com's request" in output
