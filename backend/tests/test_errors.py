"""Error reporting, read at the SDK's transport. Each scenario boots the server in its
own interpreter, because the SDK patches the whole process."""

import asyncio
import contextlib
import json
import logging
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

import httpx
import pytest
import sentry_sdk
import sentry_sdk.client
import uvicorn
from fastapi import HTTPException, WebSocket
from sentry_sdk.envelope import Envelope, Item
from sentry_sdk.transport import Transport
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

_SECRETS_KEY = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="
_DSN = "https://public-key@errors.example.com/7"
_RELEASE = "druks@9.9.9"
_SEPARATOR = b"\x1e"

# Each planted value holds LEAK, built in pieces so no secret scanner flags this file.
_GITHUB = "ghp" + "_LEAKgithub0000000000000000000000000"
_SLACK = "xoxb" + "-1111-2222-LEAKslack"
_OPENAI = "sk" + "-proj-LEAKopenai"
_ANTHROPIC = "sk" + "-ant-api03-LEAKanthropic"
_JWT = "eyJ" + "hbGciOiJub25lIn0.eyJzdWIiOiJvcCJ9.LEAKjwt"
_BEARER = "Bearer " + "LEAKbearer"
_BASIC = "Basic " + "LEAKbasic"
# Arbitrary names and repeated ones: no name decides which value goes.
_URL = "https://api.example.com/v1/items?cursor=LEAKcursor&page=LEAKpage1&page=LEAKpage2"
_FILTERED_URL = "https://api.example.com/v1/items?cursor=[Filtered]&page=[Filtered]&page=[Filtered]"
# The last two values hold an encoded & and =. No piece of them may stay as a name.
_QUERY = (
    "code=LEAKcode&state=LEAKstate1&state=LEAKstate2&anything=LEAKanything"
    "&q=salt%26LEAKpepper%3DLEAKsalt&next=%2Fcb%3Fa%3D1%26LEAKtail"
)
# The address of the client, as the reverse proxy forwards it.
_CLIENT = "198.51.100.9"
# Any request header can carry a credential, a person, or an address.
_REQUEST_HEADERS = {
    "Authorization": f"Bearer {_JWT}",
    "Cookie": "theme=LEAKcookie",
    "Referer": "https://app.example.com/callback?code=LEAKreferer",
    "X-Druks-Webhook-Token": "LEAKwebhooktoken",
    "X-ExeDev-Email": "LEAKperson@example.com",
    "X-Forwarded-For": _CLIENT,
}


class _Recorder(Transport):
    """The transport that the SDK builds in a scenario. Each envelope that it would
    send lands in a file named by the stage of the scenario."""

    stage = "boot"

    def __init__(self, options: dict) -> None:
        super().__init__(options)
        (Path(os.environ["REPORTS"]) / "transport").touch()

    def capture_envelope(self, envelope: Envelope) -> None:
        with (Path(os.environ["REPORTS"]) / self.stage).open("ab") as reports:
            reports.write(envelope.serialize() + _SEPARATOR)


def _boot() -> None:
    """Boot the web app as uvicorn does, and print whether an SDK client runs."""
    sentry_sdk.client.make_transport = _Recorder
    import druks.api.server  # noqa: F401

    print(json.dumps({"active": sentry_sdk.get_client().is_active()}))


def _record_breadcrumb() -> None:
    """Boot the web app and print what its breadcrumb hook keeps of a log line."""
    sentry_sdk.client.make_transport = _Recorder
    import druks.api.server  # noqa: F401

    before_breadcrumb = sentry_sdk.get_client().options["before_breadcrumb"]
    breadcrumb = {
        "category": "druks.probe",
        "message": f"Retrying {_URL} with {_BEARER} and {_JWT}",
        "data": {"call": {"authorization": _BASIC, "token": _GITHUB}},
    }
    print(json.dumps(before_breadcrumb(breadcrumb, {})))


def _serve() -> None:
    """Serve the web app with uvicorn, give it four probe routes, and send each one a
    request that carries credentials, a person, an address, and query values."""
    sentry_sdk.client.make_transport = _Recorder
    from druks.api.server import app

    async def handled() -> None:
        try:
            raise ConnectionError("The upstream reset the connection.")
        except ConnectionError as error:
            raise HTTPException(502, "The upstream failed.") from error

    async def logged() -> dict:
        try:
            raise ConnectionError("The upstream reset the connection.")
        except ConnectionError:
            logging.getLogger("druks.probe").exception("The upstream failed.")
        return {}

    async def unhandled() -> None:
        token = _GITHUB
        try:
            raise ConnectionError(f"The upstream refused {token}.")
        except ConnectionError:
            logging.getLogger("druks.probe").exception(
                "Retrying %s", _URL, extra={"call": {"headers": {"Authorization": _BASIC}}}
            )
        sentry_sdk.set_context("upstream", {"request": {"url": _URL, "authorization": _BASIC}})
        # The Authorization rule removes the rest of its line, so it comes last.
        raise RuntimeError(
            f"GitHub refused {token}. Slack refused {_SLACK}. OpenAI refused {_OPENAI}. "
            f"Anthropic refused {_ANTHROPIC}. The page {_URL} answered {_BEARER} and "
            f"{_JWT}. Authorization: {_BASIC}"
        )

    async def streamed(websocket: WebSocket) -> None:
        await websocket.accept()
        raise RuntimeError("The stream failed.")

    for name, endpoint in (("handled", handled), ("logged", logged), ("unhandled", unhandled)):
        app.add_api_route(f"/probes/{name}", endpoint, methods=["POST"])
    app.add_api_websocket_route("/probes/streamed", streamed)

    async def send() -> None:
        listener = socket.create_server(("127.0.0.1", 0))
        address = f"127.0.0.1:{listener.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, lifespan="off"))
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        while not server.started:
            await asyncio.sleep(0.01)
        async with httpx.AsyncClient(base_url=f"http://{address}") as client:
            for stage, status in (("handled", 502), ("logged", 200), ("unhandled", 500)):
                _Recorder.stage = stage
                response = await client.post(
                    f"/probes/{stage}?{_QUERY}",
                    headers=_REQUEST_HEADERS,
                    json={"prompt": "LEAKbody"},
                )
                assert response.status_code == status, response.text
        _Recorder.stage = "streamed"
        async with connect(
            f"ws://{address}/probes/streamed?{_QUERY}", additional_headers=_REQUEST_HEADERS
        ) as stream:
            with contextlib.suppress(ConnectionClosed):
                await stream.recv()
        server.should_exit = True
        await serving

    asyncio.run(send())


def _crash() -> None:
    """Boot the web app, let a thread fail, and then let the process fail."""
    sentry_sdk.client.make_transport = _Recorder
    import druks.api.server  # noqa: F401

    def fail() -> None:
        raise RuntimeError(f"The thread failed with {_SLACK}.")

    _Recorder.stage = "thread"
    thread = threading.Thread(target=fail)
    thread.start()
    thread.join()
    _Recorder.stage = "process"
    raise RuntimeError(f"The process failed with {_OPENAI}.")


def _run(
    scenario: str, directory: Path, *, config: str = "", **environment: str
) -> subprocess.CompletedProcess[str]:
    """Run ``scenario`` in a fresh interpreter, with ``config`` as its druks.toml. What
    its SDK would send lands in ``directory/reports``."""
    (directory / "druks.toml").write_text(config)
    (directory / "reports").mkdir()
    inherited = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("DRUKS_", "SENTRY_"))
    }
    return subprocess.run(
        [sys.executable, "-c", f"import test_errors; test_errors.{scenario}()"],
        cwd=directory,
        env={
            **inherited,
            "DRUKS_CONFIG": str(directory / "druks.toml"),
            "DRUKS_SECRETS_KEY": _SECRETS_KEY,
            "PYTHONPATH": str(Path(__file__).parent),
            "REPORTS": str(directory / "reports"),
            **environment,
        },
        capture_output=True,
        text=True,
        timeout=120,
    )


def _payload(reports: Path, stage: str) -> bytes:
    """The envelopes of ``stage``, serialized as they would leave the process."""
    path = reports / stage
    return path.read_bytes() if path.exists() else b""


def _items(reports: Path, stage: str) -> list[Item]:
    payload = _payload(reports, stage)
    envelopes = [Envelope.deserialize(chunk) for chunk in payload.split(_SEPARATOR) if chunk]
    return [item for envelope in envelopes for item in envelope.items]


def _events(reports: Path, stage: str) -> list[dict]:
    return [item.payload.json for item in _items(reports, stage) if item.type == "event"]


@pytest.fixture(scope="module")
def served(tmp_path_factory) -> Path:
    directory = tmp_path_factory.mktemp("served")
    config = """
[urls]
endpoint = "https://dashboard.example.com"
webhook_host = "hooks.example.com"
"""
    finished = _run(
        "_serve", directory, config=config, DRUKS_ERRORS_DSN=_DSN, SENTRY_RELEASE=_RELEASE
    )
    assert finished.returncode == 0, finished.stderr
    return directory / "reports"


@pytest.fixture(scope="module")
def crashed(tmp_path_factory) -> Path:
    directory = tmp_path_factory.mktemp("crashed")
    config = """
[urls]
endpoint = "https://dashboard.example.com/"
"""
    finished = _run("_crash", directory, config=config, DRUKS_ERRORS_DSN=_DSN)
    assert finished.returncode == 1, finished.stderr
    return directory / "reports"


def test_an_empty_errors_dsn_starts_no_sdk_even_with_sentry_dsn_set(tmp_path):
    finished = _run("_boot", tmp_path, SENTRY_DSN=_DSN)

    assert finished.returncode == 0, finished.stderr
    assert json.loads(finished.stdout.splitlines()[-1]) == {"active": False}
    assert not (tmp_path / "reports" / "transport").exists()


@pytest.mark.parametrize(
    "dsn",
    [
        "https://LEAKkey@errors.example.com:LEAKport/7",
        "https://LEAKkey@errors.example.com/LEAKproject",
    ],
)
def test_a_malformed_errors_dsn_refuses_boot_without_showing_the_dsn(tmp_path, dsn):
    finished = _run("_boot", tmp_path, DRUKS_ERRORS_DSN=dsn)

    assert finished.returncode == 1
    assert "MalformedDsnError: errors.dsn is not a DSN." in finished.stderr
    assert "LEAK" not in finished.stderr


def test_a_malformed_endpoint_is_not_reported_as_a_malformed_dsn(tmp_path):
    config = """
[urls]
endpoint = "https://[fd00::1/"
"""
    finished = _run("_boot", tmp_path, config=config, DRUKS_ERRORS_DSN=_DSN)

    assert finished.returncode == 1
    assert "ValueError: Invalid IPv6 URL" in finished.stderr
    assert "MalformedDsnError" not in finished.stderr


def test_the_breadcrumb_hook_scrubs_a_breadcrumb_as_the_sdk_records_it(tmp_path):
    finished = _run("_record_breadcrumb", tmp_path, DRUKS_ERRORS_DSN=_DSN)

    assert finished.returncode == 0, finished.stderr
    assert json.loads(finished.stdout.splitlines()[-1]) == {
        "category": "druks.probe",
        "message": f"Retrying {_FILTERED_URL} with Bearer [Filtered] and [Filtered]",
        "data": {"call": {"authorization": "[Filtered]", "token": "[Filtered]"}},
    }


def test_an_unhandled_web_error_sends_one_error_event(served):
    [item] = _items(served, "unhandled")
    [exception] = item.payload.json["exception"]["values"]

    assert item.type == "event"
    assert exception["type"] == "RuntimeError"
    assert exception["mechanism"] == {"type": "starlette", "handled": False}


def test_a_handled_502_and_an_error_log_send_nothing(served):
    assert not _payload(served, "handled")
    assert not _payload(served, "logged")


def test_an_error_log_goes_as_a_scrubbed_breadcrumb_of_the_next_event(served):
    [event] = _events(served, "unhandled")
    [retrying] = [
        crumb for crumb in event["breadcrumbs"]["values"] if crumb["category"] == "druks.probe"
    ]

    assert retrying["message"] == f"Retrying {_FILTERED_URL}"
    assert retrying["data"] == {"call": {"headers": {"Authorization": "[Filtered]"}}}


def test_an_event_leaves_without_credentials_query_values_locals_or_body(served):
    [event] = _events(served, "unhandled")
    request = event["request"]

    assert b"LEAK" not in _payload(served, "unhandled")
    assert request["query_string"] == (
        "code=[Filtered]&state=[Filtered]&state=[Filtered]&anything=[Filtered]"
        "&q=[Filtered]&next=[Filtered]"
    )
    assert "cookies" not in request
    assert not request.get("data")
    assert event["contexts"]["upstream"]["request"] == {
        "url": _FILTERED_URL,
        "authorization": "[Filtered]",
    }
    assert event["exception"]["values"][0]["value"] == (
        "GitHub refused [Filtered]. Slack refused [Filtered]. OpenAI refused [Filtered]. "
        f"Anthropic refused [Filtered]. The page {_FILTERED_URL} answered Bearer [Filtered] "
        "and [Filtered]. Authorization: [Filtered]"
    )
    frames = event["exception"]["values"][0]["stacktrace"]["frames"]
    assert not any("vars" in frame for frame in frames)


def test_a_request_header_off_the_allowlist_keeps_its_name_but_not_its_value(served):
    [event] = _events(served, "unhandled")
    headers = event["request"]["headers"]

    assert headers["content-type"] == "application/json"
    assert {name: headers[name.lower()] for name in _REQUEST_HEADERS} == dict.fromkeys(
        _REQUEST_HEADERS, "[Filtered]"
    )


@pytest.mark.parametrize("stage", ["unhandled", "streamed"])
def test_a_web_event_leaves_without_the_address_of_its_client(served, stage):
    [event] = _events(served, stage)
    payload = _payload(served, stage)

    assert event["exception"]["values"][-1]["type"] == "RuntimeError"
    assert _CLIENT.encode() not in payload
    assert b"LEAK" not in payload


def test_the_named_integrations_are_the_only_ones(served):
    [event] = _events(served, "unhandled")

    assert event["sdk"]["integrations"] == [
        "excepthook",
        "fastapi",
        "logging",
        "starlette",
        "threading",
    ]


def test_the_server_is_the_webhook_host_else_the_endpoint_host(served, crashed):
    [web] = _events(served, "unhandled")
    [process] = _events(crashed, "process")

    assert web["server_name"] == "hooks.example.com"
    assert process["server_name"] == "dashboard.example.com"


def test_sentry_release_names_the_release(served):
    [event] = _events(served, "unhandled")

    assert event["release"] == _RELEASE


def test_a_failed_thread_and_a_failed_process_each_send_one_event(crashed):
    [thread] = _events(crashed, "thread")
    [process] = _events(crashed, "process")

    assert thread["exception"]["values"][-1]["mechanism"]["type"] == "threading"
    assert process["exception"]["values"][-1]["mechanism"]["type"] == "excepthook"
    assert b"LEAK" not in _payload(crashed, "thread") + _payload(crashed, "process")
