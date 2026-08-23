from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from druks.api.dependencies import SettingsDep
from druks.apps.registry import webhooks

from .exceptions import InvalidWebhookError

router = APIRouter(prefix="/_external", tags=["webhooks"])


def match_webhook(path: str) -> tuple[type, dict[str, Any]]:
    """Resolve an inbound webhook path to its handler class and the URL params
    captured from the path. Scans the registered webhooks by their compiled
    pattern (a handful of providers, so a linear scan is fine). Raises
    ``InvalidWebhookError`` when nothing matches."""
    # Compiled patterns always end in ``/`` (see ``Webhook.path``), so normalize
    # here: a provider configured against the slash-less URL resolves the same.
    if not path.endswith("/"):
        path = path + "/"
    target = "/" + path
    for cls in webhooks.all():
        match = cls.pattern.match(target)
        if match:
            return cls, match.groupdict()
    raise InvalidWebhookError(f"{path!r} is not a registered webhook.")


@router.post("/{hook_path:path}")
async def dispatch(hook_path: str, request: Request, settings: SettingsDep) -> Response:
    try:
        WebhookCls, kwargs = match_webhook(hook_path)
    except InvalidWebhookError as exc:
        raise HTTPException(status_code=404, detail="Webhook doesn't exist.") from exc
    return await WebhookCls(request, kwargs, settings).respond()
