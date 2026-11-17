"""Error reporting, read at the SDK's transport. Each scenario boots the server in its
own interpreter, because the SDK patches the whole process."""

import asyncio
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
from fastapi import HTTPException
from sentry_sdk.envelope import Envelope, Item
from sentry_sdk.transport import Transport

_SECRETS_KEY = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="
_DSN = "https://public-key@errors.example.com/7"
_RELEASE = "druks@9.9.9"
_SEPARATOR = b"\x1e"

# Each credential is built in pieces, so no secret scanner flags this file.
_CAPABILITY = "4f9c2d7e1b8a6053"
_GITHUB = "ghp" + "_LEAKgithub0000000000000000000000000"
_SLACK = "xoxb" + "-1111-2222-LEAKslack"
_OPENAI = "sk" + "-proj-LEAKopenai"
_ANTHROPIC = "sk" + "-ant-api03-LEAKanthropic"
_JWT = "eyJ" + "hbGciOiJub25lIn0.eyJzdWIiOiJvcCJ9.LEAKjwt"
_BEARER = "Bearer " + "LEAKbearer"
_BASIC = "Basic " + "LEAKbasic"
# The secret of a Druks token is base64url, so it can hold a "-".
_PAT = "druks_pat_" + "AbCdEf123456_Zm9v-LEAKpat"
# Passwords that only their place in a URL gives away. The cache has no user name.
_PASSWORD = "hunter2"
_DATABASE = f"postgresql://druks:{_PASSWORD}@db:5432/druks"
_CACHE = "redis://:" + "LEAKcache@cache:6379/0"
# Arbitrary names and repeated ones: no name decides which value goes.
_URL = "https://api.example.com/v1/items?cursor=LEAKcursor&page=LEAKpage1&page=LEAKpage2"
_FILTERED_URL = "https://api.example.com/v1/items?cursor=[Filtered]&page=[Filtered]&page=[Filtered]"


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


def _serve() -> None:
    """Serve the web app with uvicorn, give it three probe routes, and send each one a
    request with the token in a header. The unhandled route also takes the token in its
    path and its query."""
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

    async def unhandled(capability: str) -> None:
        sentry_sdk.add_breadcrumb(message=f"Answering {capability}")
        # The Authorization rule removes the rest of its line, so it comes last.
        raise RuntimeError(
            f"GitHub refused {_GITHUB}. Slack refused {_SLACK}. OpenAI refused {_OPENAI}. "
            f"Anthropic refused {_ANTHROPIC}. The page {_URL} answered {_BEARER} and "
            f"{_JWT}. The database {_DATABASE} and the cache {_CACHE} refused the key "
            f"{_SECRETS_KEY} and the token {_PAT}. Authorization: {_BASIC}"
        )

    app.add_api_route("/probes/handled", handled, methods=["POST"])
    app.add_api_route("/probes/logged", logged, methods=["POST"])
    app.add_api_route("/probes/{capability}/unhandled", unhandled, methods=["POST"])

    async def send() -> None:
        listener = socket.create_server(("127.0.0.1", 0))
        server = uvicorn.Server(uvicorn.Config(app, lifespan="off"))
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        while not server.started:
            await asyncio.sleep(0.01)
        base_url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        async with httpx.AsyncClient(base_url=base_url) as client:
            for stage, path, status in (
                ("handled", "/probes/handled", 502),
                ("logged", "/probes/logged", 200),
                ("unhandled", f"/probes/{_CAPABILITY}/unhandled?capability={_CAPABILITY}", 500),
            ):
                _Recorder.stage = stage
                response = await client.post(path, headers={"X-Capability": _CAPABILITY})
                assert response.status_code == status, response.text
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


def test_an_unhandled_web_error_sends_one_error_event(served):
    [item] = _items(served, "unhandled")
    [exception] = item.payload.json["exception"]["values"]

    assert item.type == "event"
    assert exception["type"] == "RuntimeError"
    assert exception["mechanism"] == {"type": "starlette", "handled": False}


def test_a_handled_502_and_an_error_log_send_nothing(served):
    assert not _payload(served, "handled")
    assert not _payload(served, "logged")


def test_an_event_leaves_without_its_request_breadcrumbs_locals_or_credentials(served):
    payload = _payload(served, "unhandled")
    [event] = _events(served, "unhandled")
    [exception] = event["exception"]["values"]

    for planted in (_CAPABILITY, _SECRETS_KEY, _PASSWORD, "LEAK"):
        assert planted.encode() not in payload, planted
    assert "request" not in event
    assert event["breadcrumbs"] == {"values": []}
    assert not any("vars" in frame for frame in exception["stacktrace"]["frames"])
    assert event["transaction"] == "/probes/{capability}/unhandled"
    assert exception["value"] == (
        "GitHub refused [Filtered]. Slack refused [Filtered]. OpenAI refused [Filtered]. "
        f"Anthropic refused [Filtered]. The page {_FILTERED_URL} answered Bearer [Filtered] "
        "and [Filtered]. The database postgresql://druks:[Filtered]@db:5432/druks and the "
        "cache redis://:[Filtered]@cache:6379/0 refused the key [Filtered] and the token "
        "[Filtered]. Authorization: [Filtered]"
    )


def test_the_named_integrations_are_the_only_ones(served):
    [event] = _events(served, "unhandled")

    assert event["sdk"]["integrations"] == ["excepthook", "fastapi", "starlette", "threading"]


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
