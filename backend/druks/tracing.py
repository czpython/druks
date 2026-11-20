"""Traces over OTLP. One provider takes the spans of DBOS, FastAPI, SQLAlchemy, httpx,
and Druks. Each span crosses one privacy boundary on its way to the collector."""

import os
import re
from collections.abc import Sequence
from datetime import datetime
from urllib.parse import urlsplit

# Private DBOS APIs, so they depend on the DBOS version.
from dbos._dbos import _get_or_create_dbos_registry
from dbos._error import DBOSMaxStepRetriesExceeded
from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.sqlalchemy.engine import EngineTracer
from opentelemetry.metrics import get_meter
from opentelemetry.propagate import set_global_textmap
from opentelemetry.sdk import trace as sdk_trace
from opentelemetry.sdk.environment_variables import (
    OTEL_EXPORTER_OTLP_ENDPOINT,
    OTEL_EXPORTER_OTLP_TRACES_ENDPOINT,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.sampling import Decision, Sampler, SamplingResult
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.util.re import parse_env_headers
from opentelemetry.util.types import Attributes
from sqlalchemy.ext.asyncio import AsyncEngine

from druks.exceptions import DruksError
from druks.harnesses.exceptions import HarnessError
from druks.settings import Settings

# A trace for each poll of the dashboard or health check would bury the traces of the work.
_UNTRACED_REQUESTS = frozenset(
    {
        ("GET", "/api/chat/conversations"),
        ("GET", "/api/chat/conversations/{conversation_id}/active-runs"),
        ("GET", "/api/dashboard/overview"),
        ("GET", "/api/dashboard/schedules"),
        ("GET", "/api/notifications"),
        ("GET", "/api/runs/{run}"),
        ("GET", "/api/usage"),
        ("GET", "/api/usage/history"),
        ("GET", "/api/usage/today"),
        ("GET", "/health"),
    }
)

# What a span may carry out of the process: identifiers, route templates, and the facts
# that Druks names.
_ATTRIBUTES = frozenset(
    {
        "druks.agent",
        "druks.app",
        "druks.cost_usd",
        "druks.gate",
        "druks.harness",
        "druks.host",
        "druks.model",
        "druks.run_id",
        "druks.subject_id",
        "druks.subject_type",
        "druks.usage.cache_creation_tokens",
        "druks.usage.cached_input_tokens",
        "druks.usage.input_tokens",
        "druks.usage.output_tokens",
        "druks.usage.reasoning_tokens",
        "druks.usage.total_tokens",
        "service.name",
        "dbos.application.version",
        "dbos.executor.id",
        "dbos.operation.type",
        "dbos.operation.workflow_id",
        "dbos.queue.name",
        "http.method",
        "http.route",
        "http.status_code",
        "net.peer.name",
        "net.peer.port",
        "db.name",
        "db.operation",
        "db.statement",
        "db.system",
    }
)
_RESOURCE_ATTRIBUTES = frozenset({"service.name", "druks.host"})
_EXCEPTION_ATTRIBUTES = frozenset({"exception.type", "exception.message"})

# A safe message can still quote a credential.
_URL = re.compile(r"\b([a-z][a-z0-9+.-]*://)(?:[^\s/?#@]*@)?([^\s/?#'\"<>]+)[^\s'\"<>]*", re.I)
_QUERY = re.compile(r"\?[^\s'\"<>]*=[^\s'\"<>]*")
_NAMED_SECRET = re.compile(
    r"\b([\w.-]*(?:token|secret|password|api[_-]?key|authorization|cookie)[\w.-]*)"
    r"([\"']?\s*[:=]\s*[\"']?)(?:(?:bearer|basic)\s+)?[^\s'\",;&}]+",
    re.I,
)
_SCHEME_CREDENTIAL = re.compile(r"\b(bearer|basic)\s+[^\s'\",;]+", re.I)

_provider: sdk_trace.TracerProvider | None = None


def init_tracing(settings: Settings) -> None:
    """Send traces to ``traces.endpoint``, else to the endpoint that the standard OTLP
    variables name. Without an endpoint, Druks makes no provider and records no span.
    A second call changes nothing."""
    global _provider
    has_endpoint = bool(settings.traces.endpoint) or any(
        os.environ.get(name)
        for name in (OTEL_EXPORTER_OTLP_TRACES_ENDPOINT, OTEL_EXPORTER_OTLP_ENDPOINT)
    )
    if has_endpoint and not _provider:
        headers = parse_env_headers(settings.traces.headers.get_secret_value(), liberal=True)
        exporter = OTLPSpanExporter(endpoint=settings.traces.endpoint or None, headers=headers)
        _provider = sdk_trace.TracerProvider(
            sampler=_Sampler(), resource=Resource(get_trace_attributes(settings))
        )
        _provider.add_span_processor(_ExceptionRecorder())
        _provider.add_span_processor(BatchSpanProcessor(_PrivacyBoundary(exporter)))
        # DBOS makes its spans on the global provider.
        trace.set_tracer_provider(_provider)
        # The baggage of a caller can hold user data, so Druks reads none and sends none.
        set_global_textmap(TraceContextTextMapPropagator())
        HTTPXClientInstrumentor().instrument(tracer_provider=_provider)


def is_tracing() -> bool:
    return bool(_provider)


def get_trace_attributes(settings: Settings) -> dict[str, str]:
    """The service and the host of its webhooks, which tell one installation's traces
    from another's."""
    attributes = {"service.name": "druks"}
    if host := urlsplit(settings.urls.webhook_base).hostname:
        attributes["druks.host"] = host
    return attributes


def get_error_message(error: BaseException) -> str:
    """What Druks says about ``error`` outside the process: a notification's reason
    and a span's exception. An error whose message is safe says that message without
    credentials. A harness error says its class name and its code, because its message
    can quote the output of an agent. Any other error says only its class name,
    because its text can quote a request, a response, or a header. A step that ran
    out of retries says what its last attempt raised."""
    # An error can come from any code, so its class decides what it may say.
    # ast-grep-ignore: rule-3
    if isinstance(error, DBOSMaxStepRetriesExceeded):
        return get_error_message(error.errors[-1])
    # ast-grep-ignore: rule-3
    if isinstance(error, DruksError) and error.is_message_safe:
        message = _URL.sub(r"\1\2", str(error))
        message = _QUERY.sub("", message)
        message = _NAMED_SECRET.sub(r"\1\2[redacted]", message)
        return _SCHEME_CREDENTIAL.sub(r"\1 [redacted]", message)
    # ast-grep-ignore: rule-3
    if isinstance(error, HarnessError) and error.code:
        return f"{type(error).__name__}: {error.code}"
    return type(error).__name__


def instrument_app(app: FastAPI) -> None:
    """Trace each request that ``app`` serves, when Druks sends traces."""
    if _provider:
        # A streamed response sends one message for each event, and a span for each
        # would bury its request.
        FastAPIInstrumentor.instrument_app(
            app, tracer_provider=_provider, exclude_spans=["receive", "send"]
        )


def instrument_engine(engine: AsyncEngine) -> None:
    """Trace each statement that ``engine`` runs, without its parameters, when Druks
    sends traces."""
    if _provider:
        # One engine at a time: SQLAlchemyInstrumentor traces every engine, and the
        # engines of DBOS poll its tables all the time.
        EngineTracer(
            _provider.get_tracer(__name__),
            engine.sync_engine,
            get_meter(__name__).create_up_down_counter("db.client.connections.usage"),
        )


def record_gate_wait(run_id: str, gate: str, parked_at: datetime, *, is_answered: bool) -> None:
    """Trace a run's wait at ``gate``, from the park that the run recorded to now. A
    wait that ends without an answer, at a timeout or a cancel, ends in error."""
    span = trace.get_tracer(__name__).start_span(
        f"gate_wait {gate}",
        attributes={"druks.gate": gate, "druks.run_id": run_id},
        start_time=int(parked_at.timestamp() * 1_000_000_000),
    )
    if not is_answered:
        span.set_status(trace.StatusCode.ERROR)
    span.end()


def shutdown_tracing() -> None:
    """Export the spans that wait in the batch, then stop."""
    if _provider:
        _provider.shutdown()


def _get_request(attributes: Attributes) -> tuple[object, object]:
    """The method and the route template of an HTTP span."""
    attributes = attributes or {}
    return attributes.get("http.method"), attributes.get("http.route")


class _Sampler(Sampler):
    """Keep every workflow trace. Drop each untraced request with every span under it,
    and every span under a parent that its caller left unsampled. A span takes no
    trace state from its parent."""

    def should_sample(
        self,
        parent_context: Context | None,
        trace_id: int,
        name: str,
        kind: trace.SpanKind | None = None,
        attributes: Attributes = None,
        links: Sequence[trace.Link] | None = None,
        trace_state: trace.TraceState | None = None,
    ) -> SamplingResult:
        # DBOS names the span of a workflow after the workflow. A workflow started under
        # an untraced request still keeps its trace.
        if name in _get_or_create_dbos_registry().workflow_info_map:
            return SamplingResult(Decision.RECORD_AND_SAMPLE, attributes)
        if kind is trace.SpanKind.SERVER and _get_request(attributes) in _UNTRACED_REQUESTS:
            return SamplingResult(Decision.DROP)
        parent = trace.get_current_span(parent_context).get_span_context()
        if parent.is_valid and not parent.trace_flags.sampled:
            return SamplingResult(Decision.DROP)
        return SamplingResult(Decision.RECORD_AND_SAMPLE, attributes)

    def get_description(self) -> str:
        return "DruksSampler"


class _ExceptionRecorder(sdk_trace.SpanProcessor):
    """Make each span record an exception as an error report states it: its type and
    get_error_message(), with no traceback. The SDK has no hook for how a span records
    an exception, and each instrumentation records the raw one."""

    def on_start(self, span: sdk_trace.Span, parent_context: Context | None = None) -> None:
        def record_exception(
            exception: BaseException,
            attributes: Attributes = None,
            timestamp: int | None = None,
            escaped: bool = False,
        ) -> None:
            exception_type = type(exception)
            span.add_event(
                "exception",
                {
                    "exception.type": f"{exception_type.__module__}.{exception_type.__qualname__}",
                    "exception.message": get_error_message(exception),
                },
                timestamp,
            )

        span.record_exception = record_exception


class _PrivacyBoundary(SpanExporter):
    """Export only what the operator lets leave the process: identifiers, route
    templates, the facts that Druks names, and the exceptions as an error report
    states them. No header, body, query value, or URL with its user leaves."""

    def __init__(self, exporter: SpanExporter) -> None:
        self._exporter = exporter

    def export(self, spans: Sequence[sdk_trace.ReadableSpan]) -> SpanExportResult:
        redacted = []
        for span in spans:
            attributes = {
                key: value for key, value in span.attributes.items() if key in _ATTRIBUTES
            }
            # A URL leaves as its host alone, which names the service that a call reached.
            if host := urlsplit(str(span.attributes.get("http.url", ""))).hostname:
                attributes["server.address"] = host
            exceptions = [
                sdk_trace.Event(
                    "exception",
                    {
                        key: value
                        for key, value in event.attributes.items()
                        if key in _EXCEPTION_ATTRIBUTES
                    },
                    event.timestamp,
                )
                for event in span.events
                if event.name == "exception"
            ]
            status = span.status
            if status.status_code is trace.StatusCode.ERROR:
                # An instrumentation describes an error with the raw exception. The span
                # says what the exception it recorded says instead.
                description = exceptions[-1].attributes["exception.message"] if exceptions else None
                status = trace.Status(trace.StatusCode.ERROR, description)
            name = span.name
            method, route = _get_request(attributes)
            if method:
                # An HTTP span names its request by its method and route template, never
                # its path.
                name = f"{method} {route}" if route else str(method)
            resource = {
                key: value
                for key, value in span.resource.attributes.items()
                if key in _RESOURCE_ATTRIBUTES
            }
            redacted.append(
                sdk_trace.ReadableSpan(
                    name=name,
                    # A caller sets a trace state with its tracestate header, so no context
                    # leaves with one.
                    context=trace.SpanContext(
                        span.context.trace_id,
                        span.context.span_id,
                        span.context.is_remote,
                        span.context.trace_flags,
                    ),
                    parent=span.parent,
                    resource=Resource(resource, span.resource.schema_url),
                    attributes=attributes,
                    events=exceptions,
                    links=[
                        trace.Link(
                            trace.SpanContext(
                                link.context.trace_id,
                                link.context.span_id,
                                link.context.is_remote,
                                link.context.trace_flags,
                            )
                        )
                        for link in span.links
                    ],
                    kind=span.kind,
                    status=status,
                    start_time=span.start_time,
                    end_time=span.end_time,
                    instrumentation_scope=span.instrumentation_scope,
                )
            )
        return self._exporter.export(redacted)

    def shutdown(self) -> None:
        self._exporter.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._exporter.force_flush(timeout_millis)
