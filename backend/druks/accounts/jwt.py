import re
from functools import lru_cache

from fastmcp.server.auth.providers.jwt import JWTVerifier
from jsonpointer import JsonPointer

from druks.accounts.exceptions import InvalidAssertionError
from druks.settings import Settings

# RS256 is the pinned profile — an untrusted token never chooses its own
# algorithm.
_ALGORITHM = "RS256"
_ARRAY_INDEX = re.compile(r"0|[1-9][0-9]*")


@lru_cache(maxsize=1)
def _verifier(jwks_uri: str, issuer: str, audience: str) -> JWTVerifier:
    return JWTVerifier(jwks_uri=jwks_uri, issuer=issuer, audience=audience, algorithm=_ALGORITHM)


async def verify_assertion(token: str, settings: Settings) -> str:
    """The verified identity claim of an edge-minted assertion, else
    InvalidAssertionError."""
    verifier = _verifier(
        settings.identity.jwks_url,
        settings.identity.jwt_issuer,
        settings.identity.jwt_audience,
    )
    access = await verifier.verify_token(token)
    # The verifier checks signature, issuer, audience, and expiry-if-present;
    # our contract additionally requires exp and a nonblank string identity.
    if not access or "exp" not in access.claims:
        raise InvalidAssertionError("Assertion rejected.")
    claim: object = access.claims
    for part in JsonPointer(settings.identity.jwt_identity_claim).parts:
        if isinstance(claim, dict) and part in claim:
            claim = claim[part]
        elif isinstance(claim, list) and _ARRAY_INDEX.fullmatch(part) and int(part) < len(claim):
            claim = claim[int(part)]
        else:
            raise InvalidAssertionError("Assertion carries no value at the identity pointer.")
    if isinstance(claim, str) and claim.strip():
        return claim.strip()
    raise InvalidAssertionError(
        f"Assertion carries no usable {settings.identity.jwt_identity_claim} claim."
    )
