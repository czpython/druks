import json
from urllib.parse import quote, urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from druks.accounts.dependencies import current_session_account
from druks.core.apis.github import GITHUB
from druks.core.apis.slack import SLACK_CREATE_APP_URL
from druks.core.services import Github, Slack
from druks.core.templates import render_page

# Mounted by the loader under /api/core, like any app's routes.
router = APIRouter(prefix="/services/github", tags=["services"])
slack_router = APIRouter(prefix="/services/slack", tags=["services"])


@slack_router.get("/manifest")
async def create_slack_app() -> RedirectResponse:
    """Open Slack's app creation with the Slack app's manifest filled in."""
    query = urlencode({"new_app": 1, "manifest_json": json.dumps(Slack.get_manifest())})
    return RedirectResponse(f"{SLACK_CREATE_APP_URL}?{query}")


@router.get("/manifest", response_class=HTMLResponse)
async def create_github_app(request: Request) -> HTMLResponse:
    # The operator's browser lands here from the connect card. GitHub only
    # accepts a manifest via form POST, so this page carries it across; the
    # operator names an org (or leaves it blank for a personal account) and
    # continues to GitHub, which walks them through creating the App, then
    # redirects to the callback below with a one-time code.
    settings = request.app.state.settings
    endpoint = settings.urls.endpoint.rstrip("/")
    if not endpoint:
        raise HTTPException(
            status_code=409,
            detail="Set urls.endpoint to the base URL the operator's browser reaches druks "
            "at, to create the GitHub App.",
        )
    manifest = {
        **Github.manifest,
        "url": endpoint,
        "redirect_url": f"{endpoint}/api/core/services/github/manifest/callback",
        "callback_urls": [f"{endpoint}/api/oauth/callback"],
        "hook_attributes": {
            "url": f"{settings.urls.webhook_base}/_external/github/events/",
            "active": True,
        },
    }
    return render_page("github_manifest.html", manifest_json=json.dumps(manifest))


@router.get(
    "/manifest/callback",
    response_class=HTMLResponse,
    dependencies=[Depends(current_session_account)],
)
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
    row = await Github.connect(
        {
            "app_id": str(app["id"]),
            "client_id": app["client_id"],
            "client_secret": app["client_secret"],
            "private_key": app["pem"],
            "webhook_secret": app["webhook_secret"],
        }
    )
    slug = row.identity["slug"]
    install_url = f"https://github.com/apps/{quote(slug, safe='')}/installations/new"
    # druks opened this tab via window.open; the broadcast tells the connect
    # card to refetch, then the tab moves on to the one step GitHub still
    # needs — installing the App on the repositories druks should work in.
    return render_page(
        "github_manifest_callback.html", slug=slug, install_url=install_url, service=GITHUB
    )
