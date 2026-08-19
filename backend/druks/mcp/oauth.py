from urllib.parse import urlparse

import httpx
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from druks.database import db_session, get_session
from druks.mcp.constants import OAUTH_CALLBACK_PATH, OAUTH_PROVIDER
from druks.mcp.enums import IdentityMode
from druks.mcp.exceptions import GrantRefreshError, MissingGrantError, OauthConnectError
from druks.mcp.models import McpOauthGrant, McpServer
from druks.services import OauthClient, OauthExchangeError, OauthRefreshError
from druks.services.constants import OAUTH_MINT_WAIT_ATTEMPTS, OAUTH_MINT_WAIT_INTERVAL_SECONDS


def _http() -> httpx.AsyncClient:
    # One construction point so the suite can swap in a MockTransport client.
    return httpx.AsyncClient(timeout=30.0, follow_redirects=True)


def _origin(url: str) -> str:
    parts = urlparse(url)
    return f"{parts.scheme}://{parts.netloc}"


async def _get_json(client: httpx.AsyncClient, url: str) -> dict | None:
    # A discovery probe: any failure — network, non-2xx, non-JSON, non-object —
    # just means this candidate url isn't it.
    try:
        response = await client.get(url)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        return
    return payload if isinstance(payload, dict) else None


async def _discover(client: httpx.AsyncClient, name: str, server_url: str) -> dict:
    """The authorization-server metadata for an MCP server: RFC 9728
    protected-resource metadata names the authorization server, RFC 8414 (or
    OIDC) metadata names its endpoints. Well-known lookups try the path-aware
    forms first, then the root; a server predating RFC 9728 falls back to
    being its own authorization server. Both documents are verified to be
    about what we asked for — the resource must echo the server url and the
    issuer must claim itself (the RFC 9728 §3.3 / 8414 §3.3 mix-up defenses);
    a document claiming another issuer is skipped, never trusted."""
    origin = _origin(server_url)
    path = urlparse(server_url).path.rstrip("/")
    issuer = ""
    for well_known in (
        f"{origin}/.well-known/oauth-protected-resource{path}",
        f"{origin}/.well-known/oauth-protected-resource",
    ):
        resource_metadata = await _get_json(client, well_known)
        if resource_metadata and resource_metadata.get("authorization_servers"):
            claimed = resource_metadata.get("resource", "")
            if claimed.rstrip("/") != server_url.rstrip("/"):
                raise OauthConnectError(
                    name,
                    f"protected-resource metadata at {well_known} is about "
                    f"{claimed!r}, not {server_url!r}",
                )
            issuer = resource_metadata["authorization_servers"][0]
            break
    if issuer:
        candidate_issuers = [issuer]
    else:
        # No protected-resource metadata: the server is its own issuer, or its
        # origin document names another — Atlassian serves its CDN alias this
        # way — a claim with the same trust RFC 9728 gives authorization_servers.
        document = await _get_json(client, f"{origin}/.well-known/oauth-authorization-server")
        alias = document.get("issuer") if document else None
        candidate_issuers = [origin, alias] if alias and alias != origin else [origin]
    for issuer in candidate_issuers:
        if metadata := await _self_claimed_metadata(client, issuer):
            return metadata
    raise OauthConnectError(
        name,
        f"no authorization-server metadata claiming issuer {issuer} found for {server_url}",
    )


async def _self_claimed_metadata(client: httpx.AsyncClient, issuer: str) -> dict | None:
    """The issuer's metadata, only if it claims that issuer (RFC 8414 §3.3)."""
    issuer_origin = _origin(issuer)
    issuer_path = urlparse(issuer).path.rstrip("/")
    # An issuer with a path publishes under RFC 8414 insertion
    # (/.well-known/...{path}) or OIDC insertion ({path}/.well-known/...);
    # dict.fromkeys collapses the duplicates a pathless issuer produces.
    candidates = dict.fromkeys(
        (
            f"{issuer_origin}/.well-known/oauth-authorization-server{issuer_path}",
            f"{issuer_origin}/.well-known/oauth-authorization-server",
            f"{issuer_origin}/.well-known/openid-configuration{issuer_path}",
            f"{issuer_origin}{issuer_path}/.well-known/openid-configuration",
            f"{issuer_origin}/.well-known/openid-configuration",
        )
    )
    for well_known in candidates:
        metadata = await _get_json(client, well_known)
        if (
            metadata
            and metadata.get("authorization_endpoint")
            and metadata.get("token_endpoint")
            and metadata.get("issuer", "").rstrip("/") == issuer.rstrip("/")
        ):
            return metadata
    return


async def _register_client(
    client: httpx.AsyncClient, name: str, metadata: dict, redirect_uri: str
) -> dict:
    # RFC 7591 dynamic registration of a public client (PKCE, no client auth).
    # A server without a registration endpoint needs a configured client id —
    # unsupported until such a server exists.
    registration_endpoint = metadata.get("registration_endpoint", "")
    if not registration_endpoint:
        raise OauthConnectError(
            name, "the authorization server does not support dynamic client registration"
        )
    try:
        response = await client.post(
            registration_endpoint,
            json={
                "client_name": "druks",
                "redirect_uris": [redirect_uri],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            },
        )
    except httpx.HTTPError as error:
        raise OauthConnectError(name, f"client registration failed: {error}") from error
    if response.status_code not in (200, 201):
        raise OauthConnectError(name, f"client registration failed: HTTP {response.status_code}")
    try:
        registration = response.json()
    except ValueError as error:
        raise OauthConnectError(name, "client registration returned malformed JSON") from error
    if not isinstance(registration, dict) or not registration.get("client_id"):
        raise OauthConnectError(name, "client registration returned no client_id")
    return registration


async def begin_connect(
    name: str,
    server_url: str,
    endpoint: str,
    *,
    account_id: str | None,
    identity_mode: IdentityMode,
) -> str:
    """Start the operator's authorization-code + PKCE flow for one server:
    discover the authorization server, register druks as a public client, and
    hand the engine the resulting client to stash the pending exchange and
    render the consent URL. Nothing durable is written here — an abandoned
    consent simply expires."""
    redirect_uri = f"{endpoint.rstrip('/')}{OAUTH_CALLBACK_PATH}"
    async with _http() as client:
        metadata = await _discover(client, name, server_url)
        # Absent means the OAuth 2.1 baseline (S256); advertised-without-S256
        # means the flow below cannot work — fail before the consent screen.
        methods = metadata.get("code_challenge_methods_supported")
        if methods is not None and "S256" not in methods:
            raise OauthConnectError(name, "the authorization server does not support PKCE S256")
        registration = await _register_client(client, name, metadata, redirect_uri)
    return await OauthClient(
        provider=OAUTH_PROVIDER,
        authorization_endpoint=metadata["authorization_endpoint"],
        token_endpoint=metadata["token_endpoint"],
        client_id=registration["client_id"],
        client_secret=registration.get("client_secret", ""),
        # RFC 8707: bind the tokens to the MCP server they are for.
        extra_token_params={"resource": server_url},
    ).begin_connect(
        redirect_uri=redirect_uri,
        context={
            "name": name,
            "server_url": server_url,
            "account_id": account_id,
            "identity_mode": identity_mode,
        },
        extra_authorize_params={"resource": server_url},
    )


async def complete_connect(*, state: str, code: str) -> str:
    """The callback half: the shared exchange (single-use state, code +
    verifier), then the durable outcome — claim the server's identity mode and
    store the grant. Returns the server name. The grant is the only outcome —
    nothing is cached here, because it becomes real only when this request's
    transaction commits, and a cache filled ahead of that would outlive its
    failure. The first delivery mints from the committed grant."""
    try:
        tokens, pending = await OauthClient(
            provider=OAUTH_PROVIDER, http_factory=_http
        ).complete_connect(state=state, code=code)
    except OauthExchangeError as error:
        raise OauthConnectError(error.context.get("name", "unknown"), error.reason) from error
    name = pending["name"]
    # The first completed connect claims the mode: insert the row if absent,
    # fill the mode if unclaimed. A concurrent claim wins the row lock; the
    # select reads whichever choice landed, and the grant goes under it.
    session = db_session()
    session.execute(
        pg_insert(McpServer)
        .values(
            name=name,
            url=pending["server_url"],
            identity_mode=pending["identity_mode"],
        )
        .on_conflict_do_nothing(index_elements=["name"])
    )
    session.execute(
        update(McpServer)
        .where(McpServer.name == name, McpServer.identity_mode.is_(None))
        .values(identity_mode=pending["identity_mode"])
    )
    effective_identity_mode = session.scalar(
        select(McpServer.identity_mode).where(McpServer.name == name)
    )
    account_id = McpOauthGrant.get_grant_account(effective_identity_mode, pending["account_id"])
    McpOauthGrant.store(
        server_name=name,
        account_id=account_id,
        refresh_token=tokens["refresh_token"],
        token_endpoint=pending["token_endpoint"],
        resource=pending["server_url"],
        client_id=pending["client_id"],
        client_secret=pending["client_secret"],
    )
    return name


async def evict_access_token(name: str, account_id: str) -> None:
    await OauthClient(provider=OAUTH_PROVIDER).evict_access_token(f"{name}:{account_id}")


async def mint_access_token(name: str, account_id: str) -> str:
    """The delivery-side token for a connected server, minted by the shared
    engine from this server's grant — delivery never ships a server the agent
    can't authenticate to."""
    grant = McpOauthGrant.get_for_account(name, account_id)
    if not grant:
        raise MissingGrantError(name, account_id)

    def load_refresh_token() -> str:
        # Under the refresh lock: another process may have rotated and
        # committed, and this transaction may already hold the row —
        # populate_existing re-reads it past the identity map.
        fresh = (
            db_session()
            .scalars(
                select(McpOauthGrant)
                .where(McpOauthGrant.id == grant.id)
                .execution_options(populate_existing=True)
            )
            .one_or_none()
        )
        if not fresh:
            raise MissingGrantError(name, account_id)
        # The grant's secret halves are ciphertext at rest; the plaintext
        # exists only in the refresh request body.
        return fresh.refresh_token.decrypt()

    def save_refresh_token(refresh_token: str) -> None:
        # The provider invalidated the old token the moment it rotated, so
        # the write commits on its own session, never the enclosing
        # transaction — a step that rolls back later must not brick the grant.
        with get_session(db_session().get_bind()) as session:
            session.execute(
                update(McpOauthGrant)
                .where(McpOauthGrant.id == grant.id)
                .values(refresh_token=refresh_token)
            )
            session.commit()
        # Keep the enclosing transaction's copy true as well.
        grant.refresh_token = refresh_token
        db_session().flush()

    client = OauthClient(
        provider=OAUTH_PROVIDER,
        token_endpoint=grant.token_endpoint,
        client_id=grant.client_id,
        client_secret=grant.client_secret.decrypt(),
        # RFC 8707: an audience-binding server expects the refresh to carry
        # the same resource the code exchange was bound to.
        extra_token_params={"resource": grant.resource},
        mint_wait_interval_seconds=OAUTH_MINT_WAIT_INTERVAL_SECONDS,
        mint_wait_attempts=OAUTH_MINT_WAIT_ATTEMPTS,
        http_factory=_http,
    )
    try:
        return await client.mint_access_token(
            key=f"{name}:{account_id}",
            load_refresh_token=load_refresh_token,
            save_refresh_token=save_refresh_token,
        )
    except OauthRefreshError as error:
        raise GrantRefreshError(name, error.reason) from error
