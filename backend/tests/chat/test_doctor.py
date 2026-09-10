from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
from druks import doctor
from druks.contrib.chat.app import check_appliance_mcp
from druks.testing import make_settings


def test_chat_is_in_the_roster(tmp_path):
    result = doctor.check_chat(make_settings(tmp_path))

    assert result.ok
    assert result.detail == "bundled"


def test_chat_missing_from_the_roster_is_a_fault(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "iter_apps", lambda: iter(()))

    result = doctor.check_chat(make_settings(tmp_path))

    assert not result.ok
    assert not result.pending
    assert "optional" in result.detail


def test_check_chat_is_in_the_battery():
    assert doctor.check_chat in doctor.CHECKS


def _settings(*, service_url: str, endpoint: str):
    return SimpleNamespace(
        sandbox=SimpleNamespace(service_url=service_url),
        urls=SimpleNamespace(endpoint=endpoint),
    )


async def test_appliance_mcp_skips_when_sandbox_execution_is_off(monkeypatch):
    monkeypatch.setattr(
        "druks.contrib.chat.app.load_settings",
        lambda: _settings(service_url="", endpoint="http://127.0.0.1:8001"),
    )

    result = await check_appliance_mcp()

    assert result.ok
    assert result.detail.startswith("skipped")


async def test_appliance_mcp_pends_without_an_endpoint(monkeypatch):
    monkeypatch.setattr(
        "druks.contrib.chat.app.load_settings",
        lambda: _settings(service_url="http://127.0.0.1:8780", endpoint=""),
    )

    result = await check_appliance_mcp()

    assert not result.ok
    assert result.pending
    assert "/mcp" in result.detail


async def test_appliance_mcp_names_an_unreachable_url(monkeypatch):
    monkeypatch.setattr(
        "druks.contrib.chat.app.load_settings",
        lambda: _settings(service_url="http://127.0.0.1:8780", endpoint="http://druks.test:8001"),
    )

    async def fake_get(self, url):
        raise httpx.ConnectError("connection refused", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    result = await check_appliance_mcp()

    assert not result.ok
    assert not result.pending
    assert "http://druks.test:8001/mcp" in result.detail


async def test_appliance_mcp_accepts_a_live_endpoint(monkeypatch):
    monkeypatch.setattr(
        "druks.contrib.chat.app.load_settings",
        lambda: _settings(service_url="http://127.0.0.1:8780", endpoint="http://127.0.0.1:8001"),
    )

    async def fake_get(self, url):
        return MagicMock(status_code=401)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    result = await check_appliance_mcp()

    assert result.ok
    assert result.detail == "http://127.0.0.1:8001/mcp"
