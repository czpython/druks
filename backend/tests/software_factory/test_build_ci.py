import pytest
from druks.contrib.software_factory.constants import CI_REGISTER_SECONDS, CI_TTL_SECONDS
from druks.contrib.software_factory.contracts import CiReport
from druks.contrib.software_factory.datastructures import Check
from druks.core.apis.github import GitHubClient

pytestmark = pytest.mark.asyncio

HEAD = "0f3a2c1d"


def _check(name, *, conclusion="success", status="completed", app="github-actions"):
    return Check.from_check_run(
        {
            "id": 7,
            "name": name,
            "status": status,
            "conclusion": conclusion if status == "completed" else None,
            "details_url": f"https://ci/{name}",
            "html_url": f"https://github.com/acme/widget/runs/{name}",
            "app": {"slug": app},
            "output": {"summary": "1 failed", "text": None},
        }
    )


def _judge(checks, *, waited=0.0, red_on_default=frozenset(), logs=None):
    return CiReport.judge(
        HEAD, checks, waited=waited, red_on_default=set(red_on_default), logs=logs or {}
    )


async def test_a_repo_without_ci_is_green_once_no_check_registers():
    assert _judge([]) is None
    assert _judge([], waited=CI_REGISTER_SECONDS).conclusion == "green"


async def test_running_ci_is_waited_on_until_its_time_limit_blocks_it():
    running = [_check("tests", status="in_progress")]

    assert _judge(running, waited=CI_TTL_SECONDS - 1) is None
    assert _judge(running, waited=CI_TTL_SECONDS).conclusion == "blocked"


async def test_neutral_and_skipped_checks_are_green():
    checks = [_check("lint", conclusion="neutral"), _check("deploy", conclusion="skipped")]

    assert _judge(checks).conclusion == "green"


async def test_a_check_red_only_on_the_pull_request_is_red_with_its_log():
    checks = [_check("tests", conclusion="failure"), _check("e2e", conclusion="failure")]

    report = _judge(checks, red_on_default={"e2e"}, logs={"tests": "FAILED test_x"})

    assert report.conclusion == "red"
    assert [(failure.name, failure.log) for failure in report.failures] == [
        ("tests", "FAILED test_x")
    ]


async def test_checks_also_red_on_the_default_branch_block_the_round():
    report = _judge([_check("e2e", conclusion="timed_out")], red_on_default={"e2e"})

    assert report.conclusion == "blocked"
    assert "e2e" in report.reason


class _Logs(GitHubClient):
    def __init__(self, log: str):
        super().__init__("token", app_id="1", slug="druks")
        self.log = log

    async def get_job_log(self, repo, job_id):
        return self.log


async def test_a_failing_actions_job_reads_the_end_of_its_log():
    log = "\n".join(f"line {number}" for number in range(500))

    tail = await _check("tests", conclusion="failure").get_log(_Logs(log), "acme/widget")

    assert tail.splitlines()[-1] == "line 499"
    assert "line 0\n" not in tail


async def test_another_app_s_check_reports_through_its_own_output():
    check = _check("sonar", conclusion="failure", app="sonarcloud")

    assert await check.get_log(_Logs("unused"), "acme/widget") == "1 failed"
