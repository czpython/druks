from pathlib import Path

import pytest
from druks import doctor
from druks.testing import make_settings

# field_notes is the out-of-tree proof extension (``backend/tests/druks-field_notes``).
# It declares one check on its class — its summarizer API key, which passes when the
# credential is set and fails in a bare install when it's unset. These tests drive it
# through the platform's own doctor, proving an extension contributes checks through the
# loader without doctor importing the extension's private modules, and that a broken
# extension check can't hide a core one.


@pytest.fixture
def installed(monkeypatch):
    # The check reads a real env var; force it unset so the failure is deterministic
    # regardless of the developer's environment.
    monkeypatch.delenv("FIELD_NOTES_API_KEY", raising=False)


def _named(results: list[doctor.CheckResult], name: str) -> doctor.CheckResult:
    return next(result for result in results if result.name == name)


def test_passing_extension_check_reports_under_the_extension(
    installed, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A satisfied precondition passes and its result is namespaced under the extension
    name — the API-key check with the credential set."""
    monkeypatch.setenv("FIELD_NOTES_API_KEY", "sk-test")
    settings = make_settings(tmp_path)

    result = _named(doctor.check_extensions(settings), "field_notes:summary_api_key")

    assert result.ok
    assert result.detail == "set"


def test_failing_extension_check_reports_under_the_extension(installed, tmp_path: Path) -> None:
    """The extension's API-key check fails when the credential is unset, reported
    under the extension name so the operator knows which extension is broken."""
    settings = make_settings(tmp_path)

    result = _named(doctor.check_extensions(settings), "field_notes:summary_api_key")

    assert not result.ok
    assert "FIELD_NOTES_API_KEY" in result.detail


def test_unreachable_settings_database_is_an_extension_check_failure(
    installed, tmp_path: Path
) -> None:
    settings = make_settings(
        tmp_path,
        database_url="postgresql+psycopg://druks:druks@127.0.0.1:1/druks",
    )

    results = doctor.check_extensions(settings)

    result = _named(results, "ship:check_linear")
    assert not result.ok
    assert "check raised" in result.detail


def test_extension_checks_are_wired_into_the_check_battery(installed, tmp_path: Path) -> None:
    """``run_checks`` runs the extension checks: ``check_extensions`` is one of the
    battery's entries and, like ``check_harness_credentials``, fans its several
    results into the run — so the extension's checks reach the report beside core's."""
    settings = make_settings(tmp_path)

    assert doctor.check_extensions in doctor.CHECKS

    extension_results = doctor.check_extensions(settings)
    assert isinstance(extension_results, list)
    assert "field_notes:summary_api_key" in {result.name for result in extension_results}


def test_raising_extension_check_is_isolated_and_does_not_stop_siblings(
    installed, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A check that raises becomes one failing result tagged with the extension name,
    and the extension's other checks still run."""
    from druks_field_notes import extension as field_notes

    def boom() -> doctor.CheckResult:
        raise RuntimeError("provider unreachable")

    def healthy() -> doctor.CheckResult:
        return doctor.CheckResult(name="healthy", ok=True, detail="ok")

    monkeypatch.setattr(field_notes.FieldNotes, "checks", [boom, healthy])
    settings = make_settings(tmp_path)

    results = doctor.check_extensions(settings)

    raised = _named(results, "field_notes:boom")
    assert not raised.ok
    assert "provider unreachable" in raised.detail
    # The sibling check after the raising one still produced its result.
    assert _named(results, "field_notes:healthy").ok


def test_broken_extension_check_does_not_hide_core_failures(
    installed, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The key robustness contract: a raising extension check is contained inside
    ``check_extensions``, and a failing core check still reports its failure. Both
    are independent entries in ``CHECKS``, so ``run_checks`` runs them side by side —
    a broken extension can't abort or hide the core checks."""
    from druks_field_notes import extension as field_notes

    def boom() -> doctor.CheckResult:
        raise RuntimeError("kaboom")

    monkeypatch.setattr(field_notes.FieldNotes, "checks", [boom])
    # A core check that genuinely fails: redis pointed at an unreachable port.
    settings = make_settings(tmp_path, redis_url="redis://127.0.0.1:1/0")

    # Both are entries in the battery, so run_checks runs them independently.
    assert doctor.check_extensions in doctor.CHECKS
    assert doctor.check_redis in doctor.CHECKS

    # The extension's raising check is contained as a failure under its own name…
    extension_result = _named(doctor.check_extensions(settings), "field_notes:boom")
    assert not extension_result.ok
    assert "kaboom" in extension_result.detail

    # …and the core check, a separate battery entry, still runs and still fails.
    redis_result = doctor.check_redis(settings)
    assert not redis_result.ok
    assert "127.0.0.1:1" in redis_result.detail


def test_default_extension_contributes_no_checks(tmp_path: Path) -> None:
    """An extension that doesn't declare ``checks`` adds nothing because the base
    attribute is an empty list."""
    from druks.extensions import Extension

    class Plain(Extension):
        name = "plain_probe"

    assert Plain.checks == []


def test_malformed_check_return_is_contained(
    installed, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A check that returns something other than a ``CheckResult`` — a missing
    ``return`` yields ``None`` — becomes a failing result under its name rather than
    crashing the run with ``AttributeError`` and hiding later checks."""
    from druks_field_notes import extension as field_notes

    def check_forgot_return() -> doctor.CheckResult:
        return None  # type: ignore[return-value]  # the bug under test: no real return

    def healthy() -> doctor.CheckResult:
        return doctor.CheckResult(name="healthy", ok=True, detail="ok")

    # The malformed check runs before a healthy one, which must still report.
    monkeypatch.setattr(field_notes.FieldNotes, "checks", [check_forgot_return, healthy])
    settings = make_settings(tmp_path)

    results = doctor.check_extensions(settings)
    by_name = {result.name: result for result in results}

    # The malformed return is contained as a failure under its own name…
    malformed = by_name["field_notes:check_forgot_return"]
    assert not malformed.ok
    assert "CheckResult" in malformed.detail
    # …and the healthy check after it still ran and was not hidden.
    assert by_name["field_notes:healthy"].ok
