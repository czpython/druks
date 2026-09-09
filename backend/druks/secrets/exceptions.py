class SecretRevokedError(Exception):
    """A revoked vault row issues nothing."""

    def __init__(self, audience: str) -> None:
        super().__init__(f"the secret at {audience} is revoked")
        self.audience = audience
