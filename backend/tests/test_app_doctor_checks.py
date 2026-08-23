from pathlib import Path

import pytest
from druks import doctor
from druks.apps import App, AppSettings
from druks.database import db_session
from druks.testing import make_settings
from druks.user_settings.models import SettingsOverride

# field_notes is the out-of-tree proof app (``backend/tests/druks-field_notes``).
# It declares settings coherence and one check on its class. These tests drive both
# through the platform's own doctor without doctor importing the app's private
# modules, and prove that a broken app check can't hide a core one.


@pytest.fixture
def installed(monkeypatch):
    # The check reads a real env var; force it unset so the failure is deterministic
    # regardless of the developer's environment.
    monkeypatch.delenv("FIELD_NOTES_API_KEY", raising=False)


def _named(results: list[doctor.CheckResult], name: str) -> doctor.CheckResult:
    return next(result for result in results if result.name == name)


def test_passing_app_check_reports_under_the_app(
    installed, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A satisfied precondition passes and its result is namespaced under the app
    name — the API-key check with the credential set."""
    monkeypatch.setenv("FIELD_NOTES_API_KEY", "sk-test")
    settings = make_settings(tmp_path)

    result = _named(doctor.check_apps(settings), "field_notes:summary_api_key")

    assert result.ok
    assert result.detail == "set"


def test_failing_app_check_reports_under_the_app(installed, tmp_path: Path) -> None:
    """The app's API-key check fails when the credential is unset, reported
    under the app name so the operator knows which app is broken."""
    settings = make_settings(tmp_path)

    result = _named(doctor.check_apps(settings), "field_notes:summary_api_key")

    assert not result.ok
    assert "FIELD_NOTES_API_KEY" in result.detail


def test_unreachable_settings_database_is_an_app_check_failure(installed, tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path,
        database_url="postgresql+psycopg://druks:druks@127.0.0.1:1/druks",
    )

    results = doctor.check_apps(settings)

    result = _named(results, "ship:settings")
    assert not result.ok
    assert "check raised" in result.detail


def test_selected_unconnected_tracker_pends_through_ships_own_check(
    installed, tmp_path: Path, druks_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The default selector names linear; no identity is connected in this db.
    monkeypatch.setattr(doctor, "create_engine_from_url", lambda _: druks_db.get_bind())

    try:
        result = _named(doctor.check_apps(make_settings(tmp_path)), "ship:tracker")
    finally:
        db_session.registry.set(druks_db)

    assert not result.ok
    assert result.pending
    assert "linear" in result.detail


def test_half_configured_review_identity_fails_through_review_settings(
    installed, tmp_path: Path, druks_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Review identity health is Review's own: the incoherent pair fails under
    # ``review:settings`` while the set/unset check stays healthy — no core
    # doctor check hardcodes review knowledge, and no GitHub call is made.
    SettingsOverride.set_app_setting("review", "app_id", "42", is_secret=True)
    monkeypatch.setattr(doctor, "create_engine_from_url", lambda _: druks_db.get_bind())

    try:
        results = doctor.check_apps(make_settings(tmp_path))
    finally:
        db_session.registry.set(druks_db)

    settings_result = _named(results, "review:settings")
    assert not settings_result.ok
    assert settings_result.detail == (
        "Review App private key: Required once the review App ID is set."
    )
    assert _named(results, "review:identity").ok


def test_coherent_stored_settings_pass(
    installed, tmp_path: Path, druks_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    SettingsOverride.set_app_setting(
        "ship", "linear_trigger_status", "Agent Queue", is_secret=False
    )
    monkeypatch.setattr(doctor, "create_engine_from_url", lambda _: druks_db.get_bind())

    try:
        result = _named(doctor.check_apps(make_settings(tmp_path)), "ship:settings")
    finally:
        db_session.registry.set(druks_db)

    assert result.ok
    assert result.detail == "coherent"


def test_app_without_settings_has_no_settings_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Plain(App):
        name = "plain_without_settings"

    monkeypatch.setattr(doctor, "iter_apps", lambda: iter([Plain]))

    assert doctor.check_apps(make_settings(tmp_path)) == []


def test_raising_settings_clean_is_contained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Broken(App):
        name = "broken_settings"

        class Settings(AppSettings):
            def clean(self) -> dict[str, str]:
                raise RuntimeError("coherence crashed")

    monkeypatch.setattr(doctor, "iter_apps", lambda: iter([Broken]))

    result = _named(doctor.check_apps(make_settings(tmp_path)), "broken_settings:settings")

    assert not result.ok
    assert "coherence crashed" in result.detail


def test_app_checks_are_wired_into_the_check_battery(installed, tmp_path: Path) -> None:
    """``run_checks`` runs the app checks: ``check_apps`` is one of the
    battery's entries and, like ``check_harness_credentials``, fans its several
    results into the run — so the app's checks reach the report beside core's."""
    settings = make_settings(tmp_path)

    assert doctor.check_apps in doctor.CHECKS

    app_results = doctor.check_apps(settings)
    assert isinstance(app_results, list)
    assert "field_notes:summary_api_key" in {result.name for result in app_results}


def test_raising_app_check_is_isolated_and_does_not_stop_siblings(
    installed, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A check that raises becomes one failing result tagged with the app name,
    and the app's other checks still run."""
    from druks_field_notes import app as field_notes

    def boom() -> doctor.CheckResult:
        raise RuntimeError("provider unreachable")

    def healthy() -> doctor.CheckResult:
        return doctor.CheckResult(name="healthy", ok=True, detail="ok")

    monkeypatch.setattr(field_notes.FieldNotes, "checks", [boom, healthy])
    settings = make_settings(tmp_path)

    results = doctor.check_apps(settings)

    raised = _named(results, "field_notes:boom")
    assert not raised.ok
    assert "provider unreachable" in raised.detail
    # The sibling check after the raising one still produced its result.
    assert _named(results, "field_notes:healthy").ok


def test_broken_app_check_does_not_hide_core_failures(
    installed, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The key robustness contract: a raising app check is contained inside
    ``check_apps``, and a failing core check still reports its failure. Both
    are independent entries in ``CHECKS``, so ``run_checks`` runs them side by side —
    a broken app can't abort or hide the core checks."""
    from druks_field_notes import app as field_notes

    def boom() -> doctor.CheckResult:
        raise RuntimeError("kaboom")

    monkeypatch.setattr(field_notes.FieldNotes, "checks", [boom])
    # A core check that genuinely fails: redis pointed at an unreachable port.
    settings = make_settings(tmp_path, redis_url="redis://127.0.0.1:1/0")

    # Both are entries in the battery, so run_checks runs them independently.
    assert doctor.check_apps in doctor.CHECKS
    assert doctor.check_redis in doctor.CHECKS

    # The app's raising check is contained as a failure under its own name…
    app_result = _named(doctor.check_apps(settings), "field_notes:boom")
    assert not app_result.ok
    assert "kaboom" in app_result.detail

    # …and the core check, a separate battery entry, still runs and still fails.
    redis_result = doctor.check_redis(settings)
    assert not redis_result.ok
    assert "127.0.0.1:1" in redis_result.detail


def test_default_app_contributes_no_checks(tmp_path: Path) -> None:
    """An app that doesn't declare ``checks`` adds nothing because the base
    attribute is an empty list."""
    from druks.apps import App

    class Plain(App):
        name = "plain_probe"

    assert Plain.checks == []


def test_malformed_check_return_is_contained(
    installed, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A check that returns something other than a ``CheckResult`` — a missing
    ``return`` yields ``None`` — becomes a failing result under its name rather than
    crashing the run with ``AttributeError`` and hiding later checks."""
    from druks_field_notes import app as field_notes

    def check_forgot_return() -> doctor.CheckResult:
        return None  # type: ignore[return-value]  # the bug under test: no real return

    def healthy() -> doctor.CheckResult:
        return doctor.CheckResult(name="healthy", ok=True, detail="ok")

    # The malformed check runs before a healthy one, which must still report.
    monkeypatch.setattr(field_notes.FieldNotes, "checks", [check_forgot_return, healthy])
    settings = make_settings(tmp_path)

    results = doctor.check_apps(settings)
    by_name = {result.name: result for result in results}

    # The malformed return is contained as a failure under its own name…
    malformed = by_name["field_notes:check_forgot_return"]
    assert not malformed.ok
    assert "CheckResult" in malformed.detail
    # …and the healthy check after it still ran and was not hidden.
    assert by_name["field_notes:healthy"].ok
