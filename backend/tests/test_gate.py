from druks.core import workflows as harness_workflows
from druks.harnesses.datastructures import RotationResult
from druks.sandbox import gate


class _FakeRedis:
    def __init__(self):
        self.values = {}

    async def exists(self, key):
        return key in self.values

    async def set(self, key, value, **_kwargs):
        self.values[key] = value

    async def delete(self, key):
        self.values.pop(key, None)


async def test_gate_uses_redis_client(monkeypatch):
    client = _FakeRedis()
    monkeypatch.setattr(gate, "get_client", lambda: client)

    async def drained():
        return None

    monkeypatch.setattr(gate, "_drain", drained)
    await gate.wait_until_open()
    async with gate.hold():
        assert await client.exists("druks:sandbox:rotating")
    assert not await client.exists("druks:sandbox:rotating")


class _FakeHarness:
    name = "fake"
    refresh_due = True

    @classmethod
    def needs_refresh(cls, login):
        return cls.refresh_due

    @classmethod
    async def rotate_token(cls, login_id):
        return RotationResult(cls.name, "refreshed", login_id=login_id)


class _FakeLogin:
    harness = "fake"
    id = "login-1"


class _FakeLogins:
    @staticmethod
    def list_all():
        return [_FakeLogin()]


class _TrackingGate:
    """Stands in for ``gate.hold``; records whether the context was entered."""

    def __init__(self):
        self.entered = False

    def __call__(self):
        return self

    async def __aenter__(self):
        self.entered = True
        return None

    async def __aexit__(self, *exc):
        return False


async def test_refresh_closes_gate_when_a_rotation_is_due(monkeypatch):
    hold = _TrackingGate()
    monkeypatch.setattr(harness_workflows, "get_harnesses", lambda: (_FakeHarness,))
    monkeypatch.setattr(harness_workflows, "HarnessConnection", _FakeLogins)
    monkeypatch.setattr(harness_workflows.gate, "hold", hold)
    _FakeHarness.refresh_due = True

    await harness_workflows._refresh()

    assert hold.entered is True


async def test_refresh_leaves_gate_open_on_a_no_op_tick(monkeypatch):
    hold = _TrackingGate()
    monkeypatch.setattr(harness_workflows, "get_harnesses", lambda: (_FakeHarness,))
    monkeypatch.setattr(harness_workflows, "HarnessConnection", _FakeLogins)
    monkeypatch.setattr(harness_workflows.gate, "hold", hold)
    _FakeHarness.refresh_due = False

    await harness_workflows._refresh()

    assert hold.entered is False
