import json
from urllib.parse import quote

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from druks.core.apis.github import GITHUB
from druks.core.services import Github
from druks.core.templates import render_page
from druks.services.models import ServiceIdentity

# Mounted by the loader under /api/core, like any app's routes.
router = APIRouter(prefix="/github", tags=["services"])


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
    webhook_base = (
        f"https://{settings.urls.webhook_host}" if settings.urls.webhook_host else endpoint
    )
    manifest = {
        **Github.manifest,
        "url": endpoint,
        "redirect_url": f"{endpoint}/api/core/github/manifest/callback",
        "hook_attributes": {"url": f"{webhook_base}/_external/github/events/", "active": True},
    }
    return render_page("github_manifest.html", manifest_json=json.dumps(manifest))


@router.get("/manifest/callback", response_class=HTMLResponse)
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
    return render_page(
        "github_manifest_callback.html", slug=slug, install_url=install_url, service=GITHUB
    )
