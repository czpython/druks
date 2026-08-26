from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from drukbox_sdk import SandboxHost as SandboxHostRecord
from druks.sandbox import client as client_module
from druks.sandbox.host import Host


def _tailscale_record() -> SandboxHostRecord:
    return SandboxHostRecord(
        id="host-tailscale",
        name="ts",
        status="active",
        provider="exe.dev",
        image="ghcr.io/.../sandbox:test",
        external_ssh_host="ts.public.exe.xyz",
        external_ssh_port=22,
        ssh_username="root",
        internal_ssh_host="ts.tailnet.ts.net",
        known_hosts="ssh-ed25519 AAAA test-key\n",
        tailscale_device_id="dev-ts",
        private_key=None,
        last_error="",
        created_at="2026-06-09T12:00:00+00:00",
        updated_at="2026-06-09T12:00:00+00:00",
        activated_at="2026-06-09T12:00:02+00:00",
        expires_at=None,
        instance_type=None,
        disk_gb=None,
    )


def _aws_direct_record() -> SandboxHostRecord:
    return SandboxHostRecord(
        id="host-aws",
        name="ec2",
        status="active",
        provider="aws",
        image="ami-0abc12345",
        external_ssh_host="ec2-1-2-3-4.compute.amazonaws.com",
        external_ssh_port=22,
        ssh_username="ubuntu",
        internal_ssh_host=None,
        known_hosts="ssh-ed25519 BBBB test-key\n",
        tailscale_device_id=None,
        private_key="-----BEGIN OPENSSH PRIVATE KEY-----\nfakekey\n-----END OPENSSH PRIVATE KEY-----",  # noqa: E501
        last_error="",
        created_at="2026-06-09T12:00:00+00:00",
        updated_at="2026-06-09T12:00:00+00:00",
        activated_at="2026-06-09T12:00:02+00:00",
        expires_at=None,
        instance_type=None,
        disk_gb=None,
    )


@pytest.fixture(autouse=True)
def _patch_import_known_hosts(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "druks.sandbox.host.asyncssh.import_known_hosts",
        lambda raw: ("known-hosts-sentinel", raw),
    )


def test_ssh_connect_kwargs_picks_tailscale_when_internal_ssh_host_is_set():
    """Tailnet record dials MagicDNS without client keys."""
    kwargs = Host(record=_tailscale_record())._ssh_connect_kwargs()
    assert kwargs["host"] == "ts.tailnet.ts.net"
    assert kwargs["port"] == 22
    assert "client_keys" not in kwargs


def test_ssh_connect_kwargs_picks_aws_direct_when_key_is_persisted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    """AWS-shape record dials external host with the on-disk key."""
    monkeypatch.setattr(
        "druks.sandbox.host.load_settings",
        lambda: type("_S", (), {"sandbox_keys_dir": tmp_path})(),
    )
    (tmp_path / "host-aws").write_text("fakekey")
    kwargs = Host(record=_aws_direct_record())._ssh_connect_kwargs()
    assert kwargs["host"] == "ec2-1-2-3-4.compute.amazonaws.com"
    assert kwargs["port"] == 22
    assert kwargs["client_keys"] == [str(tmp_path / "host-aws")]


def test_ssh_connect_kwargs_dials_a_reattached_record_via_persisted_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    """A GET-built record never carries private_key (drukbox returns it once,
    on create) — the dial must trust the key the acquirer persisted, or every
    reattach to an AWS-direct host fails with 'no reachable address'."""
    monkeypatch.setattr(
        "druks.sandbox.host.load_settings",
        lambda: type("_S", (), {"sandbox_keys_dir": tmp_path})(),
    )
    (tmp_path / "host-aws").write_text("fakekey")
    reattached = replace(_aws_direct_record(), private_key=None)
    kwargs = Host(record=reattached)._ssh_connect_kwargs()
    assert kwargs["host"] == "ec2-1-2-3-4.compute.amazonaws.com"
    assert kwargs["client_keys"] == [str(tmp_path / "host-aws")]


def test_ssh_connect_kwargs_raises_when_the_key_was_never_persisted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    """External host but no key on disk — released or never acquired here."""
    monkeypatch.setattr(
        "druks.sandbox.host.load_settings",
        lambda: type("_S", (), {"sandbox_keys_dir": tmp_path})(),
    )
    with pytest.raises(RuntimeError, match="no reachable address"):
        Host(record=_aws_direct_record())._ssh_connect_kwargs()


def _stub_acquire_settings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    sandbox_image: str,
):
    create_calls: list[dict[str, object]] = []

    class _FakeAPI:
        async def create_host(self, **kwargs):
            create_calls.append(kwargs)
            return _aws_direct_record()

        async def delete_host(self, host_id):
            pass

        async def aclose(self):
            pass

    monkeypatch.setattr(
        client_module,
        "_upload_helper_script",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        client_module.Client,
        "_api",
        lambda _self: _FakeAPI(),
    )
    monkeypatch.setattr(
        client_module,
        "load_settings",
        lambda: type(
            "_S",
            (),
            {
                "sandbox": type("_Sandbox", (), {"image": sandbox_image})(),
                "sandbox_keys_dir": tmp_path,
            },
        )(),
    )
    return client_module.Client(), create_calls


@pytest.mark.parametrize(
    ("override", "setting", "expected"),
    [
        ("ami-top-rung", "fallback-not-used", "ami-top-rung"),
        (None, "ami-deployment-default", "ami-deployment-default"),
        (None, "", None),
    ],
    ids=["override_wins", "setting_used", "drukbox_default"],
)
async def test_acquire_image_precedence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    override: str | None,
    setting: str,
    expected: str | None,
):
    """First non-empty of (override, setting) wins; empty passes image=None,
    which the SDK drops from the request — the VM is born unpinned."""
    sc, calls = _stub_acquire_settings(monkeypatch, tmp_path, sandbox_image=setting)
    async with sc.acquire(idempotency_key="op", image_override=override):
        pass
    assert calls[0]["image"] == expected


async def test_acquire_persists_private_key_when_returned(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    """AWS-shape records land at <keys_dir>/<host_id> with mode 0600."""
    sc, _ = _stub_acquire_settings(monkeypatch, tmp_path, sandbox_image="")
    async with sc.acquire(idempotency_key="op"):
        pass
    key_path = tmp_path / "host-aws"
    assert key_path.read_text().startswith("-----BEGIN OPENSSH")
    assert oct(os.stat(key_path).st_mode & 0o777) == "0o600"


async def test_acquire_passes_template_to_drukbox(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    client, calls = _stub_acquire_settings(monkeypatch, tmp_path, sandbox_image="base")

    async with client.acquire(idempotency_key="op", template="template-1"):
        pass

    assert calls[0]["template"] == "template-1"


async def test_acquire_sends_no_template_by_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    client, calls = _stub_acquire_settings(monkeypatch, tmp_path, sandbox_image="base")

    async with client.acquire(idempotency_key="op"):
        pass

    assert calls[0]["template"] is None
