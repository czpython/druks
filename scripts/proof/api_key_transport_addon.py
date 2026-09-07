"""A mitmproxy addon for the API-key transport proof. It logs the host, path,
and credential headers of every request, answers each provider request with
401 itself, and forwards nothing that carries a credential."""

import json
import os
import time

from mitmproxy import http

LOG = os.environ.get("PROOF_LOG", "requests.jsonl")
PROVIDER_HOSTS = {"api.anthropic.com", "api.openai.com", "chatgpt.com"}
CREDENTIAL_HEADERS = ("authorization", "x-api-key", "upgrade")


def _record(kind: str, flow: http.HTTPFlow) -> None:
    entry = {
        "t": time.time(),
        "kind": kind,
        "method": flow.request.method,
        "host": flow.request.pretty_host,
        "path": flow.request.path,
        "headers": {
            name: flow.request.headers[name]
            for name in CREDENTIAL_HEADERS
            if name in flow.request.headers
        },
        "user_agent": flow.request.headers.get("user-agent"),
    }
    with open(LOG, "a") as log:
        log.write(json.dumps(entry) + "\n")


def http_connect(flow: http.HTTPFlow) -> None:
    _record("connect", flow)


def request(flow: http.HTTPFlow) -> None:
    _record("request", flow)
    if flow.request.pretty_host in PROVIDER_HOSTS:
        body = {"error": {"type": "authentication_error", "message": "observed by the proof"}}
        flow.response = http.Response.make(
            401, json.dumps(body).encode(), {"content-type": "application/json"}
        )
