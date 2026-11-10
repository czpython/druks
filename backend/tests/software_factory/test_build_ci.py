from druks.contrib.software_factory.constants import CI_REGISTER_SECONDS, CI_TTL_SECONDS
from druks.contrib.software_factory.contracts import CiReport

HEAD = "0f3a2c1d"


def _suite(*, status="completed", conclusion="success", runs=1):
    return {
        "status": status,
        "conclusion": conclusion if status == "completed" else None,
        "latest_check_runs_count": runs,
    }


def _status(state):
    return {"context": "external/lint", "state": state}


def _judge(suites, statuses=(), *, waited=0.0):
    return CiReport.judge(HEAD, list(suites), list(statuses), waited=waited)


def test_a_repo_without_ci_is_green_once_no_check_registers():
    assert _judge([]) is None
    assert _judge([], waited=CI_REGISTER_SECONDS).conclusion == "green"


def test_a_workflow_between_its_needs_waves_is_still_running():
    # The jobs of the first wave finished, and the jobs that need them have no check
    # run yet, but the workflow run's suite is still open.
    assert _judge([_suite(status="in_progress", runs=3)]) is None
    assert _judge([_suite(runs=9)]).conclusion == "green"


def test_a_suite_with_no_check_runs_counts_only_while_checks_register():
    suites = [_suite(), _suite(status="queued", runs=0)]

    assert _judge(suites) is None
    assert _judge(suites, waited=CI_REGISTER_SECONDS).conclusion == "green"


def test_running_ci_is_waited_on_until_its_time_limit():
    running_suites = [_suite(status="in_progress")]

    assert _judge(running_suites, waited=CI_TTL_SECONDS - 1) is None
    assert _judge(running_suites, waited=CI_TTL_SECONDS).conclusion == "unfinished"


def test_a_pending_commit_status_keeps_ci_running():
    assert _judge([_suite()], [_status("pending")]) is None
    assert _judge([_suite()], [_status("success")]).conclusion == "green"


def test_neutral_and_skipped_suites_are_green():
    assert _judge([_suite(conclusion="neutral"), _suite(conclusion="skipped")]).conclusion == (
        "green"
    )


def test_a_failed_suite_or_status_is_red():
    assert _judge([_suite(), _suite(conclusion="failure")]).conclusion == "red"
    assert _judge([_suite()], [_status("error")]).conclusion == "red"
