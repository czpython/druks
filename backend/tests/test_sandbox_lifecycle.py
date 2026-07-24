from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from drukbox_sdk import SandboxHost as SandboxHostRecord
from drukbox_sdk.exceptions import SandboxNotFoundError
from druks.sandbox import credentials as creds_module
from druks.sandbox import layout, repo
from druks.sandbox.client import sandbox_client
from druks.sandbox.constants import SANDBOX_HOST_LEASE_SECONDS
from druks.sandbox.datastructures import Credentials
from druks.sandbox.exceptions import ExecFailed, HostGone
from druks.sandbox.host import ExecResult


@dataclass
class _FakeUpload:
    local: Path
    remote: str


class _FakeSandbox:
    def __init__(self, host_id: str = "host-xyz") -> None:
        self.id = host_id  # mirrors real Sandbox.id (== record.id)
        self.ssh_username = "root"  # mirrors real Sandbox.ssh_username
        self.exec_log: list[tuple[list[str], float]] = []
        self.uploads: list[_FakeUpload] = []
        self.secrets: list[tuple[str, str]] = []  # (secret, remote)
        self.aclose_calls = 0
        # Per-test injection points.
        self.exec_results: dict[int, ExecResult] = {}
        self.default_exec_result = ExecResult(0, "", "")

    async def exec(self, cmd: list[str], *, timeout: float = 30.0) -> ExecResult:
        idx = len(self.exec_log)
        self.exec_log.append((cmd, timeout))
        return self.exec_results.get(idx, self.default_exec_result)

    async def upload_file(
        self,
        *,
        local: Path,
        remote: str,
        mode: int = 0o600,
    ) -> None:
        del mode  # tested at the Sandbox level, not via the fake
        self.uploads.append(_FakeUpload(local=local, remote=remote))

    async def upload_dir(
        self,
        *,
        local: Path,
        remote: str,
        excludes: tuple[str, ...] = (),
    ) -> None:
        # Record dir uploads the same way; tests assert on .remote.
        del excludes
        self.uploads.append(_FakeUpload(local=local, remote=remote))

    async def write_secret(
        self,
        *,
        secret: str,
        remote: str,
        mode: int = 0o600,
    ) -> None:
        del mode  # tested at the Sandbox level, not via the fake
        self.secrets.append((secret, remote))

    async def aclose(self) -> None:
        self.aclose_calls += 1


@dataclass
class _FakeAPI:
    created_envs: list[dict[str, str] | None] = field(default_factory=list)
    created_expires_at: list[datetime | None] = field(default_factory=list)
    deleted_ids: list[str] = field(default_factory=list)
    get_host_responses: list[SandboxHostRecord] = field(default_factory=list)
    create_record: SandboxHostRecord | None = None
    delete_raises: Exception | None = None
    # When set, every get_host call raises this exception instead of
    # returning from get_host_responses. Used by the attach() tests
    # to simulate the provider 404'ing a host we still have in our
    # local registry.
    get_host_raises: Exception | None = None

    async def create_host(
        self,
        *,
        env: dict[str, str] | None = None,
        image: str | None = None,
        idempotency_key: str | None = None,
        expires_at: datetime | None = None,
    ) -> SandboxHostRecord:
        self.created_envs.append(env)
        self.created_expires_at.append(expires_at)
        assert self.create_record is not None, "test forgot to set create_record"
        return self.create_record

    async def get_host(self, host_id: str) -> SandboxHostRecord:
        if self.get_host_raises is not None:
            raise self.get_host_raises
        if len(self.get_host_responses) > 1:
            return self.get_host_responses.pop(0)
        if self.get_host_responses:
            return self.get_host_responses[0]
        raise AssertionError("get_host_responses empty")

    async def delete_host(self, host_id: str) -> None:
        self.deleted_ids.append(host_id)
        if self.delete_raises is not None:
            raise self.delete_raises


def _record(
    status: str = "active",
    host_id: str = "host-xyz",
    last_error: str = "",
) -> SandboxHostRecord:
    return SandboxHostRecord(
        id=host_id,
        name="x",
        status=status,
        provider="exe.dev",
        image="ghcr.io/.../sandbox:test",
        external_ssh_host=f"{host_id}.ts.net",
        external_ssh_port=22,
        ssh_username="root",
        internal_ssh_host=None,
        known_hosts="ssh-ed25519 AAAA\n",
        tailscale_device_id="dev-123",
        private_key=None,
        last_error=last_error,
        created_at="2026-05-28T12:00:00+00:00",
        updated_at="2026-05-28T12:00:00+00:00",
        activated_at="2026-05-28T12:00:02+00:00",
        expires_at=None,
        instance_type=None,
        disk_gb=None,
    )


# NOTE: ``Sandbox.upload_file`` (mkdir + sftp + chmod) and
# ``Sandbox.write_secret`` (printf with shell quoting) are exercised
# against the real Sandbox in test_sandbox_host.py — the fake here
# just records the calls. Tests below cover the credentials.push
# orchestration that drives them.


async def test_push_skips_none_fields():
    sandbox = _FakeSandbox()

    await creds_module.push(
        sandbox,  # type: ignore[arg-type]
        Credentials(claude_credentials="{}"),
    )

    # Only the Claude credential, written as a secret. Codex + GitHub skipped,
    # and credentials never travel by SFTP.
    assert sandbox.uploads == []
    assert sandbox.secrets == [("{}", layout.get_claude_credentials_remote(sandbox.ssh_username))]


async def test_push_writes_all_three_when_supplied():
    sandbox = _FakeSandbox()

    await creds_module.push(
        sandbox,  # type: ignore[arg-type]
        Credentials(
            claude_credentials='{"claude": 1}',
            codex_credentials='{"codex": 1}',
            github_token="gho_xxx",
        ),
    )

    # All three are synthesized content written via write_secret — no SFTP.
    assert sandbox.uploads == []
    assert set(sandbox.secrets) == {
        ('{"claude": 1}', layout.get_claude_credentials_remote(sandbox.ssh_username)),
        ('{"codex": 1}', layout.get_codex_auth_remote(sandbox.ssh_username)),
        ("gho_xxx", layout.get_github_token_remote_path(sandbox.ssh_username)),
    }


async def test_push_uploads_extra_config_files_under_user_home(tmp_path: Path):
    sandbox = _FakeSandbox()
    sandbox.ssh_username = "exedev"  # non-root → /home/exedev
    config = tmp_path / "config.toml"
    config.write_text("[plugins]\n")

    await creds_module.push(
        sandbox,  # type: ignore[arg-type]
        Credentials(
            extra_config_files=((config, ".codex/config.toml"),),
        ),
    )

    assert sandbox.uploads[0].remote == "/home/exedev/.codex/config.toml"


async def test_push_skips_missing_extra_config_files(tmp_path: Path):
    sandbox = _FakeSandbox()

    await creds_module.push(
        sandbox,  # type: ignore[arg-type]
        Credentials(
            extra_config_files=((tmp_path / "does-not-exist.toml", ".codex/config.toml"),),
        ),
    )

    assert sandbox.uploads == []


async def test_push_uploads_extra_config_dirs_recursively(tmp_path: Path):
    sandbox = _FakeSandbox()
    sandbox.ssh_username = "exedev"
    plugins = tmp_path / "plugins" / "cache"
    plugins.mkdir(parents=True)
    (plugins / "notion").mkdir()

    await creds_module.push(
        sandbox,  # type: ignore[arg-type]
        Credentials(
            extra_config_dirs=(
                (plugins, ".claude/plugins/cache"),
                (tmp_path / "missing", ".claude/plugins/marketplaces"),  # skipped
            ),
        ),
    )

    assert [u.remote for u in sandbox.uploads] == ["/home/exedev/.claude/plugins/cache"]


async def test_clone_runs_plain_clone_fetch_and_checkout():
    sandbox = _FakeSandbox()

    await repo.clone(
        sandbox,  # type: ignore[arg-type]
        repo_url="https://github.com/owner/repo.git",
        ref="feature/branch",
    )

    # Exactly one SSH call.
    assert len(sandbox.exec_log) == 1
    cmd, _ = sandbox.exec_log[0]
    assert cmd[0] == "sh"
    body = cmd[2]
    # No token anywhere — the credential helper owns auth now. The
    # origin remote stays token-free so rotation works in place.
    assert "TOKEN=" not in body
    assert "x-access-token" not in body
    assert "/work/github-token" not in body
    # Per-user workspace root — fake sandbox SSHes as root, so /root/work.
    assert "mkdir -p /root/work" in body
    # Plain clone of the unmodified URL.
    assert "git clone https://github.com/owner/repo.git" in body
    assert f"cd {layout.get_repo_root('root')}" in body
    assert "git fetch origin feature/branch" in body
    # A NAMED local branch, not a detached HEAD — so the implementer's
    # plain ``git push`` (push.default current) lands on the PR branch.
    assert "git checkout -B feature/branch FETCH_HEAD" in body
    assert "git config push.default current" in body


async def test_clone_uses_custom_target_path():
    sandbox = _FakeSandbox()

    await repo.clone(
        sandbox,  # type: ignore[arg-type]
        repo_url="https://github.com/owner/repo.git",
        ref="main",
        target_path="/srv/checkouts/x",
    )

    cmd, _ = sandbox.exec_log[0]
    assert "mkdir -p /srv/checkouts" in cmd[2]
    assert "/srv/checkouts/x" in cmd[2]


async def test_clone_raises_exec_failed_on_clone_failure():
    sandbox = _FakeSandbox()
    sandbox.exec_results = {0: ExecResult(128, "", "fatal: not found")}

    with pytest.raises(ExecFailed, match="git clone"):
        await repo.clone(
            sandbox,  # type: ignore[arg-type]
            repo_url="https://github.com/owner/missing.git",
            ref="main",
        )


async def test_clone_redacts_token_from_error_output():
    sandbox = _FakeSandbox()
    sandbox.exec_results = {
        0: ExecResult(
            128,
            "",
            "fatal: could not read from https://x-access-token:gho_secret@github.com/x.git",
        )
    }

    with pytest.raises(ExecFailed) as excinfo:
        await repo.clone(
            sandbox,  # type: ignore[arg-type]
            repo_url="https://github.com/x/x.git",
            ref="main",
        )

    assert "gho_secret" not in str(excinfo.value)
    assert "<redacted>" in str(excinfo.value)


async def test_clone_rejects_non_github_urls():
    sandbox = _FakeSandbox()

    with pytest.raises(ValueError, match="github.com"):
        await repo.clone(
            sandbox,  # type: ignore[arg-type]
            repo_url="https://gitlab.example.com/owner/repo.git",
            ref="main",
        )


async def test_ensure_clones_when_repo_absent():
    sandbox = _FakeSandbox()
    # exec 0 = the `test -d` probe → make it fail (absent).
    sandbox.exec_results = {0: ExecResult(1, "", "")}

    await repo.ensure(
        sandbox,  # type: ignore[arg-type]
        repo_url="https://github.com/owner/repo.git",
        ref="main",
    )

    # First call probes existence; second is the clone chain.
    probe, _ = sandbox.exec_log[0]
    assert probe == ["test", "-d", f"{layout.get_repo_root('root')}/.git"]
    clone_cmd, _ = sandbox.exec_log[1]
    assert "git clone https://github.com/owner/repo.git" in clone_cmd[2]


async def test_ensure_fetches_and_checks_out_when_repo_present():
    sandbox = _FakeSandbox()  # default ExecResult(0) → probe says present

    await repo.ensure(
        sandbox,  # type: ignore[arg-type]
        repo_url="https://github.com/owner/repo.git",
        ref="feature/x",
    )

    # Probe, then a single fetch+checkout chain — no clone.
    assert sandbox.exec_log[0][0] == ["test", "-d", f"{layout.get_repo_root('root')}/.git"]
    body = sandbox.exec_log[1][0][2]
    assert "git clone" not in body
    assert f"cd {layout.get_repo_root('root')}" in body
    assert "git fetch origin feature/x" in body
    # -fB: discard warm-VM drift AND (re)point the named local branch.
    assert "git checkout -fB feature/x FETCH_HEAD" in body
    assert "git config push.default current" in body


async def test_ensure_present_with_ref_none_is_a_noop():
    sandbox = _FakeSandbox()  # probe says present

    await repo.ensure(
        sandbox,  # type: ignore[arg-type]
        repo_url="https://github.com/owner/refrepo.git",
        ref=None,
        target_path="/work/related/refrepo",
    )

    # Only the probe ran — no fetch, no clone.
    assert len(sandbox.exec_log) == 1
    assert sandbox.exec_log[0][0][0] == "test"


async def test_ensure_raises_exec_failed_on_fetch_failure():
    sandbox = _FakeSandbox()
    # Probe ok (present), fetch/checkout fails.
    sandbox.exec_results = {1: ExecResult(128, "", "fatal: bad ref")}

    with pytest.raises(ExecFailed, match="git fetch/checkout"):
        await repo.ensure(
            sandbox,  # type: ignore[arg-type]
            repo_url="https://github.com/owner/repo.git",
            ref="nope",
        )


@pytest.fixture
def patched_real_sandbox(monkeypatch: pytest.MonkeyPatch) -> list[_FakeSandbox]:
    built: list[_FakeSandbox] = []

    def make_sandbox(**kwargs: Any) -> _FakeSandbox:
        record = kwargs.get("record")
        host_id = getattr(record, "id", "host-xyz")
        fake = _FakeSandbox(host_id=host_id)
        built.append(fake)
        return fake

    # Patch in both places: client.py constructs the Sandbox (lifecycle
    # tests want the fake) and host.py is where the real class lives.
    monkeypatch.setattr("druks.sandbox.client.Sandbox", make_sandbox)
    monkeypatch.setattr("druks.sandbox.host.Sandbox", make_sandbox)
    return built


@pytest.fixture
def patched_sandbox_api(monkeypatch: pytest.MonkeyPatch) -> list[_FakeAPI]:
    """Stub ``Client._api`` to hand out a per-test fake. Tests
    append the FakeAPI they want returned (in order) and assert on it
    afterwards. Aclose is stubbed because the fake has no http to
    close."""
    apis: list[_FakeAPI] = []

    def _fake_api(self: Any) -> _FakeAPI:
        del self
        assert apis, "test did not register a _FakeAPI before triggering the client"
        return apis[-1]

    async def _fake_aclose(self: Any) -> None:  # _FakeAPI.aclose hook
        del self

    monkeypatch.setattr("druks.sandbox.client.Client._api", _fake_api)
    monkeypatch.setattr(_FakeAPI, "aclose", _fake_aclose, raising=False)
    return apis


async def test_acquire_uploads_helper_and_closes_ssh_without_releasing(
    patched_real_sandbox: list[_FakeSandbox],
    patched_sandbox_api: list[_FakeAPI],
):
    api = _FakeAPI(create_record=_record(status="active"))
    patched_sandbox_api.append(api)

    async with sandbox_client.acquire() as sandbox:
        assert sandbox.id == "host-xyz"

    # The host is created with a fixed lease so drukbox reaps it if the worker
    # dies mid-run — no druks-side reconciler.
    [expires_at] = api.created_expires_at
    assert expires_at is not None
    remaining = (expires_at - datetime.now(UTC)).total_seconds()
    assert 0 < remaining <= SANDBOX_HOST_LEASE_SECONDS

    # Helper uploaded once per acquire (long-lived hosts pay this
    # once, not per-run). The default settings SSH user is exedev,
    # so the destination is /home/exedev/druks-sandbox.
    fake = patched_real_sandbox[0]
    assert any(u.remote.endswith("/druks-sandbox") for u in fake.uploads), (
        "expected druks-sandbox helper upload"
    )
    # acquire closes the SSH connection on exit but does NOT release
    # the provider host.
    assert fake.aclose_calls == 1
    assert api.deleted_ids == []


async def test_acquire_releases_host_when_helper_upload_fails(
    patched_real_sandbox: list[_FakeSandbox],
    patched_sandbox_api: list[_FakeAPI],
):
    """Regression: if anything between ``create_host`` and the yield
    raises, ``acquire`` must release the host itself — the caller never
    learns the id, so leaving cleanup to them would orphan the VM."""

    api = _FakeAPI(create_record=_record(status="active"))
    patched_sandbox_api.append(api)

    async def _fake_upload(sandbox: Any) -> None:
        raise RuntimeError("helper upload failed")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("druks.sandbox.client._upload_helper_script", _fake_upload)
        with pytest.raises(RuntimeError, match="helper upload failed"):
            async with sandbox_client.acquire():
                pass

    assert api.deleted_ids == ["host-xyz"], (
        "create_host succeeded but the helper upload failed before yield; "
        "acquire must roll back the host it created"
    )


async def test_attach_returns_sandbox(
    patched_real_sandbox: list[_FakeSandbox],
    patched_sandbox_api: list[_FakeAPI],
):

    api = _FakeAPI(
        create_record=None,
        get_host_responses=[_record(status="active")],
    )
    patched_sandbox_api.append(api)

    async with sandbox_client.attach(host_id="host-xyz") as sandbox:
        assert sandbox.id == "host-xyz"

    # attach does not release.
    assert api.deleted_ids == []


async def test_attach_raises_host_gone_on_not_found(
    patched_real_sandbox: list[_FakeSandbox],
    patched_sandbox_api: list[_FakeAPI],
):
    api = _FakeAPI(
        create_record=None,
        get_host_raises=SandboxNotFoundError("host-xyz not found"),
    )
    patched_sandbox_api.append(api)

    with pytest.raises(HostGone):
        async with sandbox_client.attach(host_id="host-xyz"):
            pass


async def test_release_calls_sdk_delete(patched_sandbox_api: list[_FakeAPI]):

    api = _FakeAPI(create_record=None)
    patched_sandbox_api.append(api)

    await sandbox_client.release(host_id="host-xyz")

    assert api.deleted_ids == ["host-xyz"]


async def test_release_swallows_sdk_delete_failure(
    patched_sandbox_api: list[_FakeAPI],
):

    # provider 503s on delete — release must not raise, since the
    # caller's intent is "I'm done with this host"; surfacing the
    # error would force every operations.cleanup path to catch it.
    api = _FakeAPI(
        create_record=None,
        delete_raises=RuntimeError("provider 503"),
    )
    patched_sandbox_api.append(api)

    await sandbox_client.release(host_id="host-xyz")

    assert api.deleted_ids == ["host-xyz"]
