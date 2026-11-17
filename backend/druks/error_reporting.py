"""Unhandled errors go to the Sentry-compatible error tracker that ``errors.dsn`` names.
An event goes without its request, breadcrumbs, and local variables, and its strings go
without the credentials and the query values that they carry."""

import atexit
import re
from operator import attrgetter
from typing import Any
from urllib.parse import urlsplit

import sentry_sdk
from sentry_sdk.integrations.excepthook import ExcepthookIntegration
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration
from sentry_sdk.integrations.threading import ThreadingIntegration

from druks.exceptions import MalformedDsnError
from druks.settings import Settings

_FILTERED = "[Filtered]"

# An Authorization value goes whole, whatever its scheme.
_AUTHORIZATION = re.compile(r"(?i)(\bauthorization[\"']?\s*[:=]\s*[\"']?)[^\"'\r\n]+")
_BEARER = re.compile(r"(?i)(\bbearer\s+)[\w.~+/=-]+")
# The password of a URL. The user name can be empty, as in redis://:password@host.
_USERINFO = re.compile(r"(://[^/\s:@]*:)[^@\s/]+@")
_TOKEN = re.compile(
    r"""
    \beyJ[\w-]+\.[\w-]+\.[\w-]*         # JWT
    | \b(?:gh[oprsu]_|github_pat_)\w+   # GitHub
    | \b(?:xox[a-z.]*|xapp)-[\w-]+      # Slack
    | \bsk-[\w-]+                       # OpenAI and Anthropic
    | \bdruks_pat_[\w-]+                # Druks
    """,
    re.VERBOSE,
)
_QUERY = re.compile(r"\?[^\s#\"'<>]+")
_QUERY_VALUE = re.compile(r"=[^&]*")


def setup_error_reporting(settings: Settings) -> None:
    """Send unhandled errors to the tracker that ``errors.dsn`` names. An empty DSN
    starts nothing, so ``SENTRY_DSN`` cannot turn reporting on."""
    dsn = settings.errors.dsn.get_secret_value()
    if dsn:
        server_name = urlsplit(settings.urls.webhook_base).hostname
        secrets = [
            secret
            for key in Settings.secret_keys()
            if (secret := attrgetter(key)(settings).get_secret_value())
        ]
        try:
            sentry_sdk.init(
                dsn=dsn,
                server_name=server_name,
                default_integrations=False,
                auto_enabling_integrations=False,
                integrations=[
                    StarletteIntegration(failed_request_status_codes=set()),
                    FastApiIntegration(failed_request_status_codes=set()),
                    ExcepthookIntegration(),
                    ThreadingIntegration(),
                ],
                send_default_pii=False,
                include_local_variables=False,
                max_request_body_size="never",
                max_breadcrumbs=0,
                before_send=lambda event, hint: _redact_event(event, secrets),
                traces_sample_rate=None,
                profiles_sample_rate=None,
                profile_session_sample_rate=None,
                enable_logs=False,
                # The SDK has no switch for metrics, so this drops each one.
                before_send_metric=lambda metric, hint: None,
                auto_session_tracking=False,
                send_client_reports=False,
            )
        except ValueError:
            # The SDK's message quotes the part of the DSN that it cannot read.
            raise MalformedDsnError from None
        # The excepthook reports a crash after the lifespan's flush.
        atexit.register(sentry_sdk.flush)


def _redact_event(event: dict, secrets: list[str]) -> dict:
    """``event`` without its request, and with no secret, no credential, and no query
    value in the strings that stay."""
    event.pop("request", None)
    return _redact(event, secrets)


def _redact(value: Any, secrets: list[str]) -> Any:
    """``value`` with no secret, no credential, and no query value in any string, at any
    depth. ``secrets`` holds the value of each configured secret."""
    match value:
        case str():
            for secret in secrets:
                value = value.replace(secret, _FILTERED)
            value = _AUTHORIZATION.sub(rf"\1{_FILTERED}", value)
            value = _BEARER.sub(rf"\1{_FILTERED}", value)
            value = _TOKEN.sub(_FILTERED, value)
            value = _USERINFO.sub(rf"\1{_FILTERED}@", value)
            return _QUERY.sub(lambda query: _QUERY_VALUE.sub(f"={_FILTERED}", query.group()), value)
        case dict():
            return {key: _redact(item, secrets) for key, item in value.items()}
        case list() | tuple():
            return [_redact(item, secrets) for item in value]
    return value
