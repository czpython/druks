import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from operator import attrgetter

import httpx
import pytest
import requests
from conftest import capture_spans, reset_tracing
from druks import tracing
from druks.api.server import app
from druks.database import create_async_engine_from_url
from druks.harnesses.exceptions import HarnessSpendLimitError
from druks.notifications.exceptions import NotificationError
from druks.settings import get_settings
from druks.testing import TEST_DATABASE_URL, asgi_client, make_settings
from druks.tracing import get_error_message, init_tracing
from fastapi import FastAPI, Request
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from sqlalchemy import text

COLLECTOR = "https://collector.example.com/v1/traces"
STANDARD_VARIABLES = {
    "OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector.internal:4318",
    "OTEL_EXPORTER_OTLP_HEADERS": "x-api-key=env-key,x-team=env-team",
}


@pytest.fixture
def collector(monkeypatch):
    """The exports that reach the operator's collector."""
    posts = []

    def request(self, method, url, headers=None, data=None, timeout=None, allow_redirects=True):
        posts.append({"method": method, "url": url, "headers": headers, "body": data})
        response = requests.Response()
        response.status_code = 200
        return response

    monkeypatch.setattr(requests.Session, "request", request)
    return posts


@pytest.fixture
def fresh_process():
    """Leave tracing after the test as a new process finds it."""
    yield
    reset_tracing()


@pytest.fixture
def spans():
    yield capture_spans(get_settings())
    reset_tracing()


@pytest.fixture
def outside_service():
    """A service on this host, so a call leaves through the transport of httpx. It keeps
    the headers of each call that it takes."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.server.calls.append(self.headers)
            self.rfile.read(int(self.headers["Content-Length"]))
            body = b"secret-response"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.calls = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.mark.parametrize(
    ("traces", "variables", "urls", "headers"),
    [
        # Headers name no endpoint.
        ({}, {"OTEL_EXPORTER_OTLP_HEADERS": "x-api-key=env-key"}, [], {}),
        (
            {"endpoint": COLLECTOR, "headers": "x-api-key=collector-key"},
            {},
            [COLLECTOR],
            {"x-api-key": "collector-key", "x-team": None},
        ),
        (
            {},
            STANDARD_VARIABLES,
            ["http://collector.internal:4318/v1/traces"],
            {"x-api-key": "env-key", "x-team": "env-team"},
        ),
        (
            {"endpoint": COLLECTOR, "headers": "x-api-key=collector-key"},
            STANDARD_VARIABLES,
            [COLLECTOR],
            {"x-api-key": "collector-key", "x-team": "env-team"},
        ),
    ],
    ids=["no-endpoint", "traces", "standard-variables", "traces-first"],
)
async def test_traces_leave_as_protobuf_over_http_for_the_endpoint_that_is_set(
    fresh_process, collector, druks_client, monkeypatch, tmp_path, traces, variables, urls, headers
):
    for name in (
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_HEADERS",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in variables.items():
        monkeypatch.setenv(name, value)
    # Druks sends protobuf whatever the protocol says.
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/json")
    settings = make_settings(tmp_path, traces=traces, urls={"webhook_host": "hooks.example.com"})

    init_tracing(settings)
    # A second init keeps the one pipeline.
    init_tracing(settings)
    trace.get_tracer(__name__).start_span("probe").end()
    # The app sends the spans that wait in the batch when it stops.
    async with app.router.lifespan_context(app):
        pass

    assert [(post["method"], post["url"]) for post in collector] == [("POST", url) for url in urls]
    for post in collector:
        assert post["headers"]["content-type"] == "application/x-protobuf"
        assert {name: post["headers"].get(name) for name in headers} == headers
        [resource_spans] = ExportTraceServiceRequest.FromString(post["body"]).resource_spans
        resource = {
            item.key: item.value.string_value for item in resource_spans.resource.attributes
        }
        assert resource == {"service.name": "druks", "druks.host": "hooks.example.com"}
        [scope_spans] = resource_spans.scope_spans
        assert [span.name for span in scope_spans.spans] == ["probe"]


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (
            NotificationError(
                "upload to https://user:secret-password@files.example.com/v1/secret-path"
                "?token=secret-query failed. Authorization: Bearer secret-bearer. "
                '{"api_key": "secret-key"}'
            ),
            "upload to https://files.example.com failed. Authorization: [redacted] "
            '{"api_key": "[redacted]"}',
        ),
        # A harness quotes the response body of its provider.
        (
            HarnessSpendLimitError(
                'claude exited with 1. API Error: 400 {"error": {"message": "secret-customer"}}'
            ),
            "HarnessSpendLimitError: spend_limit",
        ),
    ],
    ids=["safe-message", "harness-output"],
)
async def test_a_span_leaves_with_identifiers_route_templates_and_scrubbed_errors_only(
    spans, outside_service, error, message
):
    # The engine of an installation that sends traces: database.py traces its statements.
    engine = create_async_engine_from_url(TEST_DATABASE_URL)
    probe = FastAPI()

    @probe.post("/notes/{note}")
    async def update_note(note: str, request: Request) -> None:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT :note"), {"note": note})
        async with httpx.AsyncClient() as client:
            await client.post(
                f"http://user:secret-password@127.0.0.1:{outside_service.server_port}"
                "/v1/secret-path?token=secret-query",
                headers={"Authorization": "Bearer secret-bearer"},
                content=await request.body(),
            )
        raise error from RuntimeError("password=secret-cause")

    tracing.instrument_app(probe)
    try:
        async with asgi_client(probe) as client:
            with pytest.raises(type(error)):
                await client.post(
                    "/notes/secret-note?token=secret-query",
                    headers={
                        "Authorization": "Bearer secret-bearer",
                        "Cookie": "session=secret-cookie",
                        # A caller that traces its request, with a state and a baggage.
                        "traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",
                        "tracestate": "vendor=secret-state",
                        "baggage": "user.email=secret-baggage",
                    },
                    content=b"secret-body",
                )
    finally:
        FastAPIInstrumentor.uninstrument_app(probe)
        await engine.dispose()

    exported = spans()
    assert sorted(span.name for span in exported) == ["POST", "POST /notes/{note}", "SELECT"]
    call, request, statement = sorted(exported, key=attrgetter("name"))
    assert call.attributes["server.address"] == "127.0.0.1"
    assert statement.attributes["db.statement"] == "SELECT %(note)s"
    assert request.status.description == get_error_message(error) == message
    assert [event.attributes["exception.message"] for event in request.events] == [message]
    # The collector gets every name, resource, attribute, event, link, and status here.
    assert b"secret-" not in encode_spans(exported).SerializeToString()
    # The service that Druks calls gets the trace context, and nothing that the caller set.
    [received] = outside_service.calls
    assert {name.lower() for name in received} & {"traceparent", "tracestate", "baggage"} == {
        "traceparent"
    }
