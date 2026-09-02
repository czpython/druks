import logging

from druks.core.tasks import _log_result
from druks.harnesses.datastructures import RotationResult


def test_no_credentials_stays_quiet(caplog):
    # A disconnected provider is not a refresh failure — no warning every tick.
    with caplog.at_level(logging.WARNING):
        _log_result(RotationResult("anthropic", "failed", error="no_credentials"))
    assert caplog.records == []


def test_invalid_grant_warns(caplog):
    with caplog.at_level(logging.WARNING):
        _log_result(RotationResult("anthropic", "failed", error="invalid_grant"))
    assert "token refresh failed for anthropic" in caplog.text


def test_locked_stays_quiet(caplog):
    # Another worker owns that row's refresh — losing the election is routine.
    with caplog.at_level(logging.WARNING):
        _log_result(RotationResult("anthropic", "locked", login_id="L1"))
    assert caplog.records == []
