import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from druks.apps.registry import services
from druks.harnesses.exceptions import OAuthTokenError
from druks.harnesses.models import ProviderSubscription
from druks.harnesses.providers import get_provider

from .exceptions import IdentityDenied
from .models import SandboxIdentity

logger = logging.getLogger(__name__)

# The identity bearer is the one credential here. Dashboard cookies, personal
# access tokens, and the edge identity do not authorize a fetch.
_bearer_scheme = HTTPBearer(auto_error=False, scheme_name="sandboxIdentity")

router = APIRouter(prefix="/api/secrets", tags=["secrets"])


@router.get("/{identity_id}/{name}", include_in_schema=False)
async def secret(
    identity_id: str,
    name: str,
    bearer: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> JSONResponse:
    """The value behind a box's placeholder. A 403 carries no reason. A 503
    says the source holds nothing valid now, and the exchange retries."""
    credential = bearer.credentials if bearer else ""
    try:
        identity = await SandboxIdentity.authenticate(identity_id, credential, name)
        # The row is the whole selection: the service or the subscription, and
        # the resource. Nothing in the request can pick another.
        secret = identity.get_secret(name)
        if secret.service:
            value, expires_at = await services.get(secret.service).issue_token(secret.resource)
        else:
            subscription = await ProviderSubscription.get(secret.subscription_id)
            if not subscription:
                raise OAuthTokenError("no_credentials", "the subscription is disconnected")
            token = await get_provider(subscription.provider).issue_token(
                subscription.id, except_host_id=identity.host_id or ""
            )
            value, expires_at = token.access_token, token.expires_at
        # The identity can die during the source I/O.
        await SandboxIdentity.authenticate(identity_id, credential, name)
    except IdentityDenied:
        raise HTTPException(status_code=403) from None
    except OAuthTokenError:
        raise HTTPException(status_code=503) from None
    except Exception as exc:  # noqa: BLE001 — the source failed; the box gets a 503 and the exchange retries
        logger.warning("fetch of %s for identity %s failed: %s", name, identity_id, exc)
        raise HTTPException(status_code=503) from None
    return JSONResponse(
        {"value": value, "expires_at": expires_at.isoformat() if expires_at else None},
        headers={"Cache-Control": "no-store"},
    )
