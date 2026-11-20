import asyncio
from types import SimpleNamespace

from druks.contrib.software_factory.constants import RERUN_POLL_SECONDS
from druks.contrib.software_factory.contracts import CiReport
from druks.contrib.software_factory.journal import BuildJournal
from druks.contrib.software_factory.workflows import Build
from druks.core.services import Github

HEAD = "0f3a2c1d"


def _run(run_id, *, conclusion, attempt=1, status="completed"):
    return {"id": run_id, "conclusion": conclusion, "run_attempt": attempt, "status": status}


class _FakeGithub:
    """A GitHub client that answers list_workflow_runs from a script of pages and
    records each rerun."""

    def __init__(self, pages, *, rerun=True):
        self._pages = list(pages)
        self.listed = 0
        self.reruns = []
        self._rerun = rerun

    async def list_workflow_runs(self, repo, head_sha):
        page = self._pages[min(self.listed, len(self._pages) - 1)]
        self.listed += 1
        return page

    async def rerun_failed_jobs(self, repo, run_id):
        self.reruns.append(run_id)
        return self._rerun


def _rerun_flow(monkeypatch, github):
    flow = Build()
    flow.subject = SimpleNamespace(repo="org/repo")

    async def get_client(repo):
        return github

    monkeypatch.setattr(Github, "get_client", get_client)
    return flow


# _wrap_steps replaces the @step method with a wrapper that needs a running workflow;
# __wrapped__ is the body, which a test drives on its own.
async def _rerun(flow, head_sha):
    return await Build._rerun_failed_jobs.__wrapped__(flow, head_sha)


async def test_a_non_actions_red_has_no_run_to_rerun(monkeypatch):
    # druks-house-rules posts the only red; its check has no Actions run, so the runs
    # on HEAD are green and nothing is rerun.
    github = _FakeGithub([[_run(1, conclusion="success")]])
    flow = _rerun_flow(monkeypatch, github)

    assert await _rerun(flow, HEAD) is False
    assert github.reruns == []


async def test_a_403_sends_the_red_to_implement(monkeypatch):
    github = _FakeGithub([[_run(1, conclusion="failure")]], rerun=False)
    flow = _rerun_flow(monkeypatch, github)

    assert await _rerun(flow, HEAD) is False
    assert github.reruns == [1]


async def test_the_rerun_waits_for_the_new_attempt_before_it_returns(monkeypatch):
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    github = _FakeGithub(
        [
            [_run(1, conclusion="failure", attempt=1)],
            [_run(1, conclusion="failure", attempt=1)],
            [_run(1, conclusion=None, attempt=2, status="queued")],
        ]
    )
    flow = _rerun_flow(monkeypatch, github)

    assert await _rerun(flow, HEAD) is True
    assert github.reruns == [1]
    # It read the runs until the rerun's attempt went up, so the next CI read sees the
    # rerun, not the old red.
    assert github.listed == 3
    assert slept == [RERUN_POLL_SECONDS]


def test_the_rerun_is_a_durable_step_so_a_replay_does_not_rerun_twice():
    # _wrap_steps memoizes a @step's result, so a workflow replay reads the recorded
    # value instead of rerunning the jobs. A plain method would have no __wrapped__.
    assert Build._rerun_failed_jobs.__wrapped__.__name__ == "_rerun_failed_jobs"


def _phase_flow(monkeypatch, *, ci, rerun):
    flow = Build()
    flow.subject = SimpleNamespace(repo="org/repo")
    flow.journal = BuildJournal()
    flow._settings = Build.Settings(plan_gate="human")
    counts = SimpleNamespace(implement=0, rerun=0, wait=0)
    heads = iter(["aaa", "bbb", "ccc", "ddd"])
    verdicts = iter(ci)

    async def create_branch():
        pass

    async def implement():
        counts.implement += 1
        return SimpleNamespace(head_sha=next(heads))

    async def wait_for_ci(head_sha):
        counts.wait += 1
        return CiReport(head_sha=head_sha, conclusion=next(verdicts))

    async def rerun_failed_jobs(head_sha):
        counts.rerun += 1
        return rerun

    async def review_work():
        return SimpleNamespace(decision="approve")

    async def work_gate():
        return True

    monkeypatch.setattr(flow, "_create_branch", create_branch)
    monkeypatch.setattr(flow, "implement", implement)
    monkeypatch.setattr(flow, "_wait_for_ci", wait_for_ci)
    monkeypatch.setattr(flow, "_rerun_failed_jobs", rerun_failed_jobs)
    monkeypatch.setattr(flow, "_review_work", review_work)
    monkeypatch.setattr(flow, "_work_gate", work_gate)
    return flow, counts


async def test_a_flaky_red_is_cleared_by_the_rerun_without_a_revision(monkeypatch):
    # Red, the rerun clears it to green, so the round reviews and ends — one implement.
    flow, counts = _phase_flow(monkeypatch, ci=["red", "green"], rerun=True)

    await flow._implement_phase()

    assert (counts.implement, counts.rerun, counts.wait) == (1, 1, 2)


async def test_a_red_that_survives_the_rerun_goes_back_to_implement(monkeypatch):
    # Red, rerun, still red → a second implement call; its head is green and reviews.
    flow, counts = _phase_flow(monkeypatch, ci=["red", "red", "green"], rerun=True)

    await flow._implement_phase()

    assert (counts.implement, counts.rerun, counts.wait) == (2, 1, 3)
