from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from druks.harnesses.exceptions import OAuthTokenError
from druks.harnesses.models import ProviderSubscription
from druks.harnesses.providers import get_provider

from .exceptions import GrantDenied
from .models import SandboxGrant

# The grant bearer is the one credential here. Dashboard cookies, personal
# access tokens, and the edge identity do not authorize a fetch.
_bearer_scheme = HTTPBearer(auto_error=False, scheme_name="sandboxGrant")

router = APIRouter(prefix="/api/secrets", tags=["secrets"])


@router.get("/{grant_id}/{service}", include_in_schema=False)
async def secret(
    grant_id: str,
    service: str,
    bearer: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> JSONResponse:
    """The value behind a box's placeholder. A 403 carries no reason. A 503
    says the source holds nothing valid now, and the exchange retries."""
    credential = bearer.credentials if bearer else ""
    try:
        grant = await SandboxGrant.authenticate(grant_id, credential, service)
        subscription = await ProviderSubscription.get(grant.services[service])
        if not subscription:
            raise HTTPException(status_code=503)
        token = await get_provider(subscription.provider).issue_token(
            subscription.id, except_host_id=grant.host_id or ""
        )
        # The grant can die during the source I/O.
        await SandboxGrant.authenticate(grant_id, credential, service)
    except GrantDenied:
        raise HTTPException(status_code=403) from None
    except OAuthTokenError:
        raise HTTPException(status_code=503) from None
    expires_at = token.expires_at.isoformat() if token.expires_at else None
    return JSONResponse(
        {"value": token.access_token, "expires_at": expires_at},
        headers={"Cache-Control": "no-store"},
    )
