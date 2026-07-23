from fastapi import APIRouter, Body, Depends, HTTPException, Request

from druks.accounts.constants import PAT_NAME_LENGTH
from druks.accounts.dependencies import current_account_or_setup, current_session_account
from druks.accounts.models import Account, PersonalAccessToken
from druks.accounts.schemas import AccountResponse, IdentityResponse, PatResponse
from druks.harnesses.models import HarnessConnection

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.get("/me", response_model=IdentityResponse, response_model_by_alias=True)
async def get_identity(
    request: Request, account: Account | None = Depends(current_account_or_setup)
) -> IdentityResponse:
    return IdentityResponse(
        auth_mode=request.app.state.settings.auth_mode,
        account=AccountResponse.model_validate(account) if account else None,
        # An account needs onboarding exactly while it has no harness
        # connection; none/zero is onboarding before the account exists.
        onboarding_required=not (account and HarnessConnection.list_for_account(account.id)),
    )


@router.get("/personal-tokens", response_model=list[PatResponse], response_model_by_alias=True)
async def list_pats(
    account: Account = Depends(current_session_account),
) -> list[PersonalAccessToken]:
    return PersonalAccessToken.list_for_account(account.id)


@router.post("/personal-tokens")
async def create_pat(
    account: Account = Depends(current_session_account),
    name: str = Body(..., embed=True),
) -> dict[str, str]:
    name = name.strip()
    if name and len(name) <= PAT_NAME_LENGTH:
        # The plaintext, handed back exactly once — only its hash is stored,
        # and the new row surfaces through the list.
        _, token = PersonalAccessToken.create(account_id=account.id, name=name)
        return {"token": token}
    raise HTTPException(
        status_code=422,
        detail=f"A token needs a name of at most {PAT_NAME_LENGTH} characters.",
    )


@router.delete(
    "/personal-tokens/{pat_id}", response_model=PatResponse, response_model_by_alias=True
)
async def revoke_pat(
    pat_id: str, account: Account = Depends(current_session_account)
) -> PersonalAccessToken:
    pat = PersonalAccessToken.get(pat_id)
    if pat and pat.account_id == account.id:
        pat.revoke()
        return pat
    # One shape for missing and foreign — existence stays account-scoped.
    raise HTTPException(status_code=404, detail="No such token.")
