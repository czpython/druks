import asyncio

from drukbox_sdk import SandboxTemplate

from druks.apps import loader
from druks.durable.activity import set_run_phase

from .client import sandbox_client
from .datastructures import Sandbox
from .exceptions import TemplateNotFound, TemplateUnavailable

_TEMPLATE_POLL_SECONDS = 5


def get_declared_sandboxes(*, extra: tuple[Sandbox, ...] = ()) -> dict[str, Sandbox]:
    declared = {sandbox.setup_script_hash: sandbox for sandbox in extra}
    for app in loader.iter_apps():
        for workflow in app.workflows():
            if sandbox := workflow.sandbox:
                declared[sandbox.setup_script_hash] = sandbox
    return declared


async def prepare_sandbox_templates(*, extra: tuple[Sandbox, ...] = ()) -> None:
    for sandbox in get_declared_sandboxes(extra=extra).values():
        label = f"{sandbox.module}:{sandbox.setup}"
        template = await sandbox_client.create_template(
            setup_script=sandbox.read_setup_script().decode("utf-8"),
            provider=sandbox.provider,
            base_image=sandbox.get_base_image() or None,
            label=label,
        )
        template = await wait_for_build(sandbox, template)
        if template.status != "available":
            raise TemplateUnavailable(
                f"sandbox template {label} failed to build: {template.last_error}"
            )


async def wait_for_build(sandbox: Sandbox, template: SandboxTemplate) -> SandboxTemplate:
    base_image = sandbox.get_base_image()
    while template.status == "building":
        await asyncio.sleep(_TEMPLATE_POLL_SECONDS)
        template = await sandbox_client.get_template(
            base_image=base_image, setup_script_hash=sandbox.setup_script_hash
        )
    return template


async def get_template_id(sandbox: Sandbox, *, wait: bool = True) -> str:
    setup_script_hash = sandbox.setup_script_hash
    base_image = sandbox.get_base_image()
    try:
        template = await sandbox_client.get_template(
            base_image=base_image, setup_script_hash=setup_script_hash
        )
    except TemplateNotFound as error:
        raise TemplateUnavailable(
            f"sandbox template {setup_script_hash} is missing. Run `druks sandboxes build`."
        ) from error

    if template.status == "building" and wait:
        await set_run_phase("sandbox_building")
        template = await wait_for_build(sandbox, template)

    if template.status == "available":
        return template.id

    raise TemplateUnavailable(
        f"sandbox template {setup_script_hash} has status {template.status!r}. "
        "Run `druks sandboxes build`, and fix the setup if the build fails."
    )
