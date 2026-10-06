import re
from functools import cache

from fastmcp.server.auth.providers.jwt import JWTVerifier
from jsonpointer import JsonPointer

from druks.accounts.exceptions import InvalidAssertionError
from druks.settings import Settings

# RS256 is the pinned profile — an untrusted token never chooses its own
# algorithm.
_ALGORITHM = "RS256"
_ARRAY_INDEX = re.compile(r"0|[1-9][0-9]*")


@cache
def _verifier(jwks_uri: str, issuer: str, audience: str) -> JWTVerifier:
    return JWTVerifier(jwks_uri=jwks_uri, issuer=issuer, audience=audience, algorithm=_ALGORITHM)


async def verify_claims(token: str, *, jwks_url: str, issuer: str, audience: str) -> dict:
    """The claims of a token that the keys at ``jwks_url`` signed for ``issuer`` and
    ``audience``, else InvalidAssertionError."""
    access = await _verifier(jwks_url, issuer, audience).verify_token(token)
    # The verifier checks signature, issuer, audience, and expiry-if-present;
    # our contract additionally requires exp.
    if access and "exp" in access.claims:
        return access.claims
    raise InvalidAssertionError("Assertion rejected.")


async def verify_assertion(token: str, settings: Settings) -> str:
    """The verified identity claim of an edge-minted assertion, else
    InvalidAssertionError."""
    claim = await verify_claims(
        token,
        jwks_url=settings.identity.jwks_url,
        issuer=settings.identity.jwt_issuer,
        audience=settings.identity.jwt_audience,
    )
    # Objects and arrays only: jsonpointer would index a string's characters.
    for part in JsonPointer(settings.identity.jwt_identity_claim).parts:
        if isinstance(claim, dict):
            claim = claim.get(part)
        elif isinstance(claim, list) and _ARRAY_INDEX.fullmatch(part) and int(part) < len(claim):
            claim = claim[int(part)]
        else:
            claim = None
    if isinstance(claim, str) and claim.strip():
        return claim.strip()
    raise InvalidAssertionError(
        f"Assertion carries no usable {settings.identity.jwt_identity_claim} claim."
    )
