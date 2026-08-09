import html
import json
import logging
from urllib.parse import quote

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from druks.core.apis.github import GITHUB, GitHubClient
from druks.service_identities.exceptions import ServiceNotConnectedError
from druks.service_identities.models import ServiceIdentity
from druks.service_identities.schemas import GitHubConnectRequest, GitHubIdentityResponse
from druks.settings import load_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/service-identities", tags=["service-identities"])

# What the created App is: the single operator identity documented in
# docs/configuration.md — keep the two in step.
_GITHUB_APP_MANIFEST = {
    "name": "druks",
    "description": "Druks operator — receives webhooks, writes branches, PRs, and comments.",
    "public": False,
    "default_events": ["issue_comment", "pull_request", "pull_request_review", "push"],
    "default_permissions": {
        "metadata": "read",
        "contents": "write",
        "pull_requests": "write",
        "issues": "write",
        "checks": "read",
        "statuses": "read",
    },
}


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


@router.get("/github/manifest", response_class=HTMLResponse)
async def create_github_app(request: Request, org: str = "") -> HTMLResponse:
    # The operator's browser lands here from the connect card. GitHub only
    # accepts a manifest via form POST, so this page carries it across and
    # submits itself; GitHub walks the operator through creating the App,
    # then redirects to the callback below with a one-time code.
    settings = request.app.state.settings
    endpoint = settings.urls.endpoint.rstrip("/")
    if not endpoint:
        raise HTTPException(
            status_code=409,
            detail="Set urls.endpoint to the base URL the operator's browser reaches druks "
            "at, to create the GitHub App.",
        )
    webhook_base = (
        f"https://{settings.urls.webhook_host}" if settings.urls.webhook_host else endpoint
    )
    manifest = {
        **_GITHUB_APP_MANIFEST,
        "url": endpoint,
        "redirect_url": f"{endpoint}/api/service-identities/github/manifest/callback",
        "hook_attributes": {"url": f"{webhook_base}/_external/github/events/", "active": True},
    }
    create_url = "https://github.com/settings/apps/new"
    if org:
        create_url = f"https://github.com/organizations/{quote(org, safe='')}/settings/apps/new"
    return HTMLResponse(
        "<html><body>"
        f'<form method="post" action="{html.escape(create_url)}">'
        f'<input type="hidden" name="manifest" value="{html.escape(json.dumps(manifest))}">'
        "<p>Continuing to GitHub to create the App — "
        '<button type="submit">continue manually</button> if nothing happens.</p>'
        "</form><script>document.forms[0].submit()</script></body></html>"
    )


@router.get("/github/manifest/callback", response_class=HTMLResponse)
async def github_manifest_callback(request: Request, code: str = "") -> HTMLResponse:
    if not code:
        raise HTTPException(status_code=400, detail="Missing code in the GitHub redirect.")
    api_url = request.app.state.settings.github_api_url
    async with httpx.AsyncClient() as client:
        converted = await client.post(
            f"{api_url}/app-manifests/{quote(code, safe='')}/conversions",
            headers={"Accept": "application/vnd.github+json"},
        )
    if converted.is_error:
        # Codes are single-use and expire within the hour; the only fix is a
        # fresh pass through the create page.
        raise HTTPException(
            status_code=400,
            detail="GitHub rejected the creation code — restart from Create GitHub App.",
        )
    app = converted.json()
    slug = app["slug"]
    ServiceIdentity.connect(
        GITHUB,
        identity={"app_id": str(app["id"]), "slug": slug},
        secrets={"private_key": app["pem"], "webhook_secret": app["webhook_secret"]},
    )
    install_url = f"https://github.com/apps/{quote(slug, safe='')}/installations/new"
    # druks opened this tab via window.open; the broadcast tells the connect
    # card to refetch, then the tab moves on to the one step GitHub still
    # needs — installing the App on the repositories druks should work in.
    return HTMLResponse(
        "<html><body>"
        f"<p>Created GitHub App <b>{html.escape(slug)}</b>. "
        f'Next, <a href="{html.escape(install_url)}">install it on your repositories</a>.</p>'
        f"<script>new BroadcastChannel('druks-github-connect').postMessage({json.dumps(slug)});"
        f"window.location.replace({json.dumps(install_url)})</script></body></html>"
    )
