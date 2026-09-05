import asyncio
from pathlib import PurePosixPath

from druks.apps import loader
from druks.durable.activity import set_run_phase
from druks.settings import load_settings

from .client import sandbox_client
from .datastructures import Sandbox
from .exceptions import TemplateNotFound, TemplateUnavailable

_TEMPLATE_POLL_SECONDS = 5


def get_declared_sandboxes() -> dict[str, Sandbox]:
    declared = {}
    for app in loader.iter_apps():
        for workflow in app.workflows():
            if sandbox := workflow.sandbox:
                declared[sandbox.setup_script_hash] = sandbox
    return declared


async def prepare_sandbox_templates() -> None:
    base_image = load_settings().sandbox.image
    for sandbox in get_declared_sandboxes().values():
        app_name = loader.resolve_workflow_app(sandbox.module)
        label = f"{app_name}-{PurePosixPath(sandbox.setup).stem}".replace("_", "-")
        await sandbox_client.create_template(
            setup_script=sandbox.read_setup_script().decode("utf-8"),
            base_image=base_image or None,
            label=label,
        )


async def get_template_id(sandbox: Sandbox) -> str:
    setup_script_hash = sandbox.setup_script_hash
    base_image = load_settings().sandbox.image
    try:
        template = await sandbox_client.get_template(
            base_image=base_image, setup_script_hash=setup_script_hash
        )
    except TemplateNotFound as error:
        raise TemplateUnavailable(
            f"sandbox template {setup_script_hash} is missing. "
            "Reinstall the app or run `druks doctor`."
        ) from error

    if template.status == "building":
        await set_run_phase("sandbox_building")
        while template.status == "building":
            await asyncio.sleep(_TEMPLATE_POLL_SECONDS)
            template = await sandbox_client.get_template(
                setup_script_hash=setup_script_hash, base_image=base_image
            )

    if template.status == "available":
        return template.id

    raise TemplateUnavailable(
        f"sandbox template {setup_script_hash} has status {template.status!r}. "
        "Fix its setup and run `druks doctor`."
    )
