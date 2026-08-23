#!/usr/bin/env python3
import argparse
import importlib
import json
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Capture a headed Chromium login and import it into the Druks session vault."
    )
    parser.add_argument(
        "--name", required=True, help="Declared browser-session name, for example night_watch.acme."
    )
    parser.add_argument("--site-url", required=True, help="Login page to open in Chromium.")
    parser.add_argument(
        "--druks-url",
        default="http://127.0.0.1:8001",
        help="Druks dashboard URL (default: http://127.0.0.1:8001).",
    )
    parser.add_argument(
        "--header",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="Session-authenticated request header, usually Cookie=...; repeat as needed.",
    )
    return parser.parse_args()


def request_json(
    base_url: str,
    path: str,
    *,
    headers: dict[str, str],
    method: str = "GET",
    data: bytes | None = None,
    content_type: str = "application/json",
) -> Any:
    request_headers = {"Accept": "application/json", **headers}
    if data is not None:
        request_headers["Content-Type"] = content_type
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=data,
        headers=request_headers,
        method=method,
    )
    try:
        response = urllib.request.urlopen(request, timeout=120)
    except urllib.error.HTTPError as error:
        body = error.read().decode(errors="replace")
        try:
            detail = json.loads(body)["detail"]
        except (json.JSONDecodeError, KeyError, TypeError):
            detail = body or error.reason
        raise RuntimeError(f"Druks API rejected {method} {path}: {detail}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"Could not reach Druks at {base_url}: {error.reason}") from error
    with response:
        body = response.read()
    try:
        return json.loads(body) if body else None
    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"Druks returned a non-JSON response for {method} {path}; "
            "check the dashboard URL and session headers."
        ) from error


def load_playwright() -> Any:
    try:
        return importlib.import_module("playwright.sync_api").sync_playwright
    except ModuleNotFoundError as error:
        raise RuntimeError(
            "Playwright is required. Run `pip install playwright`, then "
            "`playwright install chromium`."
        ) from error


def main() -> int:
    args = parse_args()
    headers: dict[str, str] = {}
    for raw_header in args.header:
        name, separator, value = raw_header.partition("=")
        if not separator or not name.strip() or not value:
            raise ValueError(f"Header must be NAME=VALUE, got {raw_header!r}.")
        if name.strip().lower() == "authorization":
            raise ValueError("Browser-session mutations reject Authorization bearer tokens.")
        headers[name.strip()] = value

    sync_playwright = load_playwright()
    sessions = request_json(args.druks_url, "/api/browser-sessions", headers=headers)
    if args.name not in {row["name"] for row in sessions if row["isDeclared"]}:
        raise RuntimeError(
            f"Browser session {args.name!r} is not declared by any installed "
            "app; declare it on the App class first."
        )

    with tempfile.TemporaryDirectory(prefix="druks-browser-session-") as temporary_directory:
        storage_state_path = Path(temporary_directory) / "storage-state.json"
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=False)
            context = browser.new_context()
            page = context.new_page()
            page.goto(args.site_url, wait_until="domcontentloaded")
            input("Complete the login in Chromium, then press Enter here to import it: ")
            context.storage_state(path=str(storage_state_path))
            browser.close()

        request_json(
            args.druks_url,
            f"/api/browser-sessions/{urllib.parse.quote(args.name)}/state"
            "?payloadFormat=storage_state",
            headers=headers,
            method="PUT",
            data=storage_state_path.read_bytes(),
            content_type="application/octet-stream",
        )

    print(f"Imported {args.name}.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1) from error
