"""Unhandled errors go to the Sentry-compatible error tracker that ``errors.dsn`` names,
without the credentials and the query values they carry."""

import atexit
import re
from typing import Any
from urllib.parse import urlsplit

import sentry_sdk
from sentry_sdk.integrations.excepthook import ExcepthookIntegration
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.logging import LoggingIntegration, ignore_logger
from sentry_sdk.integrations.starlette import StarletteIntegration
from sentry_sdk.integrations.threading import ThreadingIntegration

from druks.exceptions import MalformedDsnError
from druks.settings import Settings

_FILTERED = "[Filtered]"

# An Authorization value goes whole, whatever its scheme.
_AUTHORIZATION = re.compile(r"(?i)(\bauthorization[\"']?\s*[:=]\s*[\"']?)[^\"'\r\n]+")
_BEARER = re.compile(r"(?i)(\bbearer\s+)[\w.~+/=-]+")
_TOKEN = re.compile(
    r"""
    \beyJ[\w-]+\.[\w-]+\.[\w-]*         # JWT
    | \b(?:gh[oprsu]_|github_pat_)\w+   # GitHub
    | \b(?:xox[a-z.]*|xapp)-[\w-]+      # Slack
    | \bsk-[\w-]+                       # OpenAI and Anthropic
    """,
    re.VERBOSE,
)
_QUERY_IN_TEXT = re.compile(r"\?[^\s#\"'<>]+")
_QUERY_VALUE = re.compile(r"=[^&]*")
# The SDK's fields that hold a query with no "?".
_QUERY_FIELDS = frozenset({"query_string", "http.query"})
_ALLOWED_HEADERS = frozenset({"accept", "content-length", "content-type", "host", "user-agent"})


def setup_error_reporting(settings: Settings) -> None:
    """Send unhandled errors to the tracker that ``errors.dsn`` names. An empty DSN
    starts nothing, so ``SENTRY_DSN`` cannot turn reporting on."""
    dsn = settings.errors.dsn.get_secret_value()
    if dsn:
        server_name = urlsplit(settings.urls.webhook_base).hostname
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
                    LoggingIntegration(event_level=None),
                ],
                send_default_pii=False,
                include_local_variables=False,
                max_request_body_size="never",
                before_send=lambda event, hint: _redact(event),
                before_breadcrumb=lambda breadcrumb, hint: _redact(breadcrumb),
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
        # uvicorn's log lines name the address of each client.
        ignore_logger("uvicorn.access")
        ignore_logger("uvicorn.error")
        # The excepthook reports a crash after the lifespan's flush.
        atexit.register(sentry_sdk.flush)


class RawQuery:
    """Gives each error event of a request the query string as the client sent it."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("query_string"):
            query = scope["query_string"].decode("latin-1")

            def restore(event: dict, hint: dict) -> dict:
                event["request"]["query_string"] = query
                return event

            sentry_sdk.get_isolation_scope().add_event_processor(restore)
        await self.app(scope, receive, send)


def _redact(value: Any, field: str = "") -> Any:
    """``value`` with no credential, no query value, and no value of a header off the
    allowlist, at any depth. ``field`` is the key that holds ``value``."""
    if field.lower().endswith("authorization"):
        return _FILTERED
    match value:
        case str() if field in _QUERY_FIELDS:
            return _redact_query(value)
        case str():
            value = _AUTHORIZATION.sub(rf"\1{_FILTERED}", value)
            value = _BEARER.sub(rf"\1{_FILTERED}", value)
            value = _TOKEN.sub(_FILTERED, value)
            return _QUERY_IN_TEXT.sub(lambda query: _redact_query(query.group()), value)
        case dict() if field.lower() == "headers":
            return {
                name: _redact(item, str(name))
                if str(name).lower() in _ALLOWED_HEADERS
                else _FILTERED
                for name, item in value.items()
            }
        case dict():
            return {key: _redact(item, str(key)) for key, item in value.items()}
        case list() | tuple():
            return [_redact(item, field) for item in value]
    return value


def _redact_query(query: str) -> str:
    return _QUERY_VALUE.sub(f"={_FILTERED}", query)
