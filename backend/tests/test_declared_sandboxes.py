import hashlib
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import druks.agents as agent_module
import druks.workflows as workflow_module
import pytest
from druks.contrib.ship.app import _PHASE_META
from druks.sandbox import datastructures, templates
from druks.sandbox.client import Client
from druks.sandbox.datastructures import Sandbox, get_content_hash
from druks.sandbox.exceptions import TemplateNotFound, TemplateUnavailable
from druks.workflows import Workflow


def test_sandbox_resolves_package_bytes_and_hashes_base_with_script(monkeypatch, tmp_path):
    package = tmp_path / "site_builder"
    (package / "sandboxes").mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "sandboxes" / "build.sh").write_bytes(b"#!/bin/sh\ninstall-tool\n")
    monkeypatch.syspath_prepend(tmp_path)
    monkeypatch.setattr(
        datastructures,
        "loader",
        SimpleNamespace(
            resolve_workflow_app=lambda module: "site_builder",
            get_app=lambda name: SimpleNamespace(name="site_builder", package="site_builder"),
        ),
    )
    monkeypatch.setattr(
        datastructures,
        "load_settings",
        lambda: SimpleNamespace(sandbox=SimpleNamespace(image="base-image")),
    )

    class BuildSite:
        sandbox = Sandbox(setup="sandboxes/build.sh")

    script = BuildSite.sandbox.read_setup_script()

    assert script.startswith(b"#!/bin/sh\n")
    assert BuildSite.sandbox.content_hash == hashlib.sha256(b"base-image\0" + script).hexdigest()


def test_get_declared_sandboxes_deduplicates_by_content(monkeypatch):
    shared = Sandbox(setup="sandboxes/setup.sh")

    class First:
        kind = "notes.first"
        sandbox = shared

    class Second:
        kind = "notes.second"
        sandbox = shared

    app = SimpleNamespace(workflows=lambda: [First, Second])
    monkeypatch.setattr(templates, "loader", SimpleNamespace(iter_apps=lambda: [app]))
    monkeypatch.setattr(Sandbox, "read_setup_script", lambda self: b"setup")
    monkeypatch.setattr(
        datastructures,
        "load_settings",
        lambda: SimpleNamespace(sandbox=SimpleNamespace(image="base")),
    )

    declared = templates.get_declared_sandboxes()

    assert declared == {get_content_hash("base", b"setup"): shared}


def test_ship_maps_the_sandbox_building_phase():
    assert _PHASE_META["sandbox_building"].label == "Building sandbox…"
    assert _PHASE_META["sandbox_building"].kind == "infra"


async def test_prepare_sandbox_templates_requests_each_declaration(monkeypatch):
    sandbox = Sandbox(setup="sandboxes/setup.sh")
    requirements_hash = get_content_hash("base", b"setup")
    create_template = AsyncMock()
    monkeypatch.setattr(Sandbox, "read_setup_script", lambda self: b"setup")
    monkeypatch.setattr(
        templates,
        "load_settings",
        lambda: SimpleNamespace(sandbox=SimpleNamespace(image="base")),
    )
    monkeypatch.setattr(
        templates,
        "get_declared_sandboxes",
        lambda: {requirements_hash: sandbox},
    )
    monkeypatch.setattr(
        templates,
        "sandbox_client",
        SimpleNamespace(create_template=create_template),
    )

    await templates.prepare_sandbox_templates()

    create_template.assert_awaited_once_with(
        base_image="base",
        script=b"setup",
        requirements_hash=requirements_hash,
    )



async def test_get_template_id_uses_available_template(monkeypatch):
    sandbox = Sandbox(setup="sandboxes/setup.sh")
    template = SimpleNamespace(id="template-1", status="available")
    get_template = AsyncMock(return_value=template)
    monkeypatch.setattr(Sandbox, "read_setup_script", lambda self: b"setup")
    monkeypatch.setattr(
        datastructures,
        "load_settings",
        lambda: SimpleNamespace(sandbox=SimpleNamespace(image="base")),
    )
    monkeypatch.setattr(
        templates,
        "sandbox_client",
        SimpleNamespace(get_template=get_template),
    )

    assert await templates.get_template_id(sandbox) == "template-1"
    get_template.assert_awaited_once_with(requirements_hash=get_content_hash("base", b"setup"))


async def test_get_template_id_waits_with_visible_phase(monkeypatch):
    sandbox = Sandbox(setup="sandboxes/setup.sh")
    get_template = AsyncMock(
        side_effect=[
            SimpleNamespace(id="template-1", status="building"),
            SimpleNamespace(id="template-1", status="available"),
        ]
    )
    set_run_phase = AsyncMock()
    sleep = AsyncMock()
    monkeypatch.setattr(Sandbox, "read_setup_script", lambda self: b"setup")
    monkeypatch.setattr(
        datastructures,
        "load_settings",
        lambda: SimpleNamespace(sandbox=SimpleNamespace(image="base")),
    )
    monkeypatch.setattr(
        templates,
        "sandbox_client",
        SimpleNamespace(get_template=get_template),
    )
    monkeypatch.setattr(templates, "set_run_phase", set_run_phase)
    monkeypatch.setattr(templates.asyncio, "sleep", sleep)

    assert await templates.get_template_id(sandbox) == "template-1"
    set_run_phase.assert_awaited_once_with("sandbox_building")
    sleep.assert_awaited_once_with(templates._TEMPLATE_POLL_SECONDS)
    assert get_template.await_count == 2


async def test_get_template_id_rejects_missing_template(monkeypatch):
    sandbox = Sandbox(setup="sandboxes/setup.sh")
    monkeypatch.setattr(Sandbox, "read_setup_script", lambda self: b"setup")
    monkeypatch.setattr(
        datastructures,
        "load_settings",
        lambda: SimpleNamespace(sandbox=SimpleNamespace(image="base")),
    )
    monkeypatch.setattr(
        templates,
        "sandbox_client",
        SimpleNamespace(get_template=AsyncMock(side_effect=TemplateNotFound("missing"))),
    )

    with pytest.raises(TemplateUnavailable, match="missing.*druks doctor"):
        await templates.get_template_id(sandbox)


async def test_get_template_id_rejects_failed_template(monkeypatch):
    sandbox = Sandbox(setup="sandboxes/setup.sh")
    monkeypatch.setattr(Sandbox, "read_setup_script", lambda self: b"setup")
    monkeypatch.setattr(
        datastructures,
        "load_settings",
        lambda: SimpleNamespace(sandbox=SimpleNamespace(image="base")),
    )
    monkeypatch.setattr(
        templates,
        "sandbox_client",
        SimpleNamespace(
            get_template=AsyncMock(return_value=SimpleNamespace(id="template-1", status="failed"))
        ),
    )

    with pytest.raises(TemplateUnavailable, match="failed.*druks doctor"):
        await templates.get_template_id(sandbox)


async def test_warm_lease_uses_workflow_template(monkeypatch):
    sandbox = Sandbox(setup="sandboxes/setup.sh")
    host = SimpleNamespace(
        id="host-1",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    provision = AsyncMock(return_value=host)
    resolve = AsyncMock(return_value="template-1")
    workflow = Workflow.__new__(Workflow)
    workflow.steps_reuse_sandbox = True
    workflow.sandbox = sandbox
    workflow._workflow_id = "run-1"
    workflow._host = None
    monkeypatch.setattr(
        workflow_module,
        "sandbox_client",
        SimpleNamespace(provision=provision),
    )
    monkeypatch.setattr(workflow_module, "get_template_id", resolve)
    monkeypatch.setattr(workflow_module, "set_run_phase", AsyncMock())

    assert await workflow._lease_host() == "host-1"
    resolve.assert_awaited_once_with(sandbox)
    provision.assert_awaited_once_with(
        idempotency_key="run-1:sandbox",
        template="template-1",
    )


async def test_ephemeral_lease_uses_workflow_template(monkeypatch):
    sandbox = Sandbox(setup="sandboxes/setup.sh")
    calls = []
    box = SimpleNamespace(id="host-1")

    @asynccontextmanager
    async def ephemeral(**kwargs):
        calls.append(kwargs)
        yield box

    workflow = SimpleNamespace(
        sandbox=sandbox,
        get_workspace=AsyncMock(return_value="workspace"),
    )
    resolve = AsyncMock(return_value="template-1")
    monkeypatch.setattr(
        agent_module,
        "sandbox_client",
        SimpleNamespace(ephemeral=ephemeral),
    )
    monkeypatch.setattr(agent_module, "get_template_id", resolve)

    async with agent_module._runner(workflow, None, "run-1", "summarize") as runner:
        assert runner == "workspace"

    resolve.assert_awaited_once_with(sandbox)
    assert calls == [{"idempotency_key": "run-1:summarize", "template": "template-1"}]


async def test_client_template_primitives_use_sdk_contract(monkeypatch):
    created = SimpleNamespace(id="template-1", status="building")
    listed = SimpleNamespace(
        id="template-1",
        status="available",
        requirements_hash="requirements-1",
    )

    class FakeAPI:
        def __init__(self):
            self.create_template = AsyncMock(return_value=created)
            self.list_templates = AsyncMock(return_value=[listed])
            self.aclose = AsyncMock()

    api = FakeAPI()
    client = Client()
    monkeypatch.setattr(Client, "_api", lambda self: api)

    assert (
        await client.create_template(
            base_image="base",
            script=b"setup",
            requirements_hash="requirements-1",
        )
        is created
    )
    assert await client.get_template(requirements_hash="requirements-1") is listed
    api.create_template.assert_awaited_once_with(
        base_image="base",
        script=b"setup",
        requirements_hash="requirements-1",
    )
    api.list_templates.assert_awaited_once_with()
    assert api.aclose.await_count == 2
