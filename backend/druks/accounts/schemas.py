from datetime import datetime

from pydantic import ConfigDict, Field

from druks.schemas import BaseResponse


class AccountResponse(BaseResponse):
    model_config = ConfigDict(from_attributes=True)

    id: str
    username: str


class IdentityResponse(BaseResponse):
    # What /api/auth/me answers: how this deployment authenticates, who the
    # request resolved to (null in the none/zero setup state), and whether that
    # identity still needs its first harness connection.
    auth_mode: str
    account: AccountResponse | None
    onboarding_required: bool


class PatResponse(BaseResponse):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    # The token's visible handle — the secret is unrecoverable, so the prefix
    # is how a row is identified in the list and how a token string found in
    # the wild maps back to what to revoke.
    prefix: str = Field(validation_alias="token_prefix")
    created_at: datetime
    expires_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None
    status: str
