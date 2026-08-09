import logging

from fastapi import APIRouter, HTTPException

from druks.core.apis.github import GITHUB, GitHubClient
from druks.service_identities.exceptions import ServiceNotConnectedError
from druks.service_identities.models import ServiceIdentity
from druks.service_identities.schemas import GitHubConnectRequest, GitHubIdentityResponse
from druks.settings import load_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/service-identities", tags=["service-identities"])


@router.get("/github", response_model=GitHubIdentityResponse, response_model_by_alias=True)
async def get_github_identity() -> GitHubIdentityResponse:
    try:
        return GitHubIdentityResponse.from_row(ServiceIdentity.get(GITHUB))
    except ServiceNotConnectedError:
        return GitHubIdentityResponse.from_row(None)


@router.post("/github", response_model=GitHubIdentityResponse, response_model_by_alias=True)
async def connect_github(payload: GitHubConnectRequest) -> GitHubIdentityResponse:
    app_id = payload.app_id.strip()
    if not app_id or not payload.private_key.strip() or not payload.webhook_secret:
        raise HTTPException(
            status_code=422,
            detail="App ID, PEM private key, and webhook secret are all required.",
        )
    try:
        # Live-authenticate the pasted credentials as the App and take the
        # slug GitHub reports — proves the App ID matches the PEM before
        # anything may displace a working identity.
        client = GitHubClient(
            app_id=app_id,
            private_key=payload.private_key,
            base_url=load_settings().github_api_url,
        )
        slug = await client.get_authenticated_app_slug()
    except Exception as error:  # noqa: BLE001 — any auth/parse failure is a rejected paste
        # A fixed message: the failure detail could quote request internals,
        # and nothing pasted may echo back.
        logger.warning("GitHub service-identity connect rejected: %s", type(error).__name__)
        raise HTTPException(
            status_code=422,
            detail="GitHub did not accept these credentials — check the App ID and PEM key.",
        ) from error
    row = ServiceIdentity.connect(
        GITHUB,
        identity={"app_id": app_id, "slug": slug},
        secrets={"private_key": payload.private_key, "webhook_secret": payload.webhook_secret},
    )
    return GitHubIdentityResponse.from_row(row)
