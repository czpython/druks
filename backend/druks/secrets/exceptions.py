from druks.exceptions import DruksError


class MissingSecretError(DruksError):
    """The source has no secret to issue until a person acts: a revoked row, a
    missing grant, or an App that is not on the repo. The message names the fix."""


class SecretRevokedError(MissingSecretError):
    """A revoked vault row issues nothing."""

    def __init__(self, audience: str) -> None:
        super().__init__(f"the secret at {audience} is revoked")
        self.audience = audience
