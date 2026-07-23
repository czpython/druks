from functools import lru_cache

from fastmcp.server.auth.providers.jwt import JWTVerifier

from druks.accounts.exceptions import InvalidAssertionError
from druks.settings import Settings

# RS256 is the pinned profile — an untrusted token never chooses its own
# algorithm.
_ALGORITHM = "RS256"


@lru_cache(maxsize=1)
def _verifier(jwks_uri: str, issuer: str, audience: str) -> JWTVerifier:
    return JWTVerifier(jwks_uri=jwks_uri, issuer=issuer, audience=audience, algorithm=_ALGORITHM)


async def verify_assertion(token: str, settings: Settings) -> str:
    """The verified identity claim of an edge-minted assertion, else
    InvalidAssertionError."""
    verifier = _verifier(
        settings.auth_jwks_url, settings.auth_jwt_issuer, settings.auth_jwt_audience
    )
    access = await verifier.verify_token(token)
    # The verifier checks signature, issuer, audience, and expiry-if-present;
    # our contract additionally requires exp and a nonblank string identity.
    if not access or "exp" not in access.claims:
        raise InvalidAssertionError("Assertion rejected.")
    claim = access.claims.get(settings.auth_jwt_identity_claim)
    if isinstance(claim, str) and claim.strip():
        return claim.strip()
    raise InvalidAssertionError(
        f"Assertion carries no usable {settings.auth_jwt_identity_claim} claim."
    )
