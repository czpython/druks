import asyncio

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
                declared[sandbox.content_hash] = sandbox
    return declared


async def prepare_sandbox_templates() -> None:
    base_image = load_settings().sandbox.image
    for requirements_hash, sandbox in get_declared_sandboxes().items():
        await sandbox_client.create_template(
            base_image=base_image,
            script=sandbox.read_setup_script(),
            requirements_hash=requirements_hash,
        )


async def get_template_id(sandbox: Sandbox) -> str:
    requirements_hash = sandbox.content_hash
    try:
        template = await sandbox_client.get_template(requirements_hash=requirements_hash)
    except TemplateNotFound as error:
        raise TemplateUnavailable(
            f"sandbox template {requirements_hash} is missing. "
            "Reinstall the app or run `druks doctor`."
        ) from error

    if template.status == "building":
        await set_run_phase("sandbox_building")
        while template.status == "building":
            await asyncio.sleep(_TEMPLATE_POLL_SECONDS)
            template = await sandbox_client.get_template(requirements_hash=requirements_hash)

    if template.status == "available":
        return template.id

    raise TemplateUnavailable(
        f"sandbox template {requirements_hash} has status {template.status!r}. "
        "Fix its setup and run `druks doctor`."
    )
