from fastapi import APIRouter, Body, Depends, HTTPException, Request

from druks.accounts.constants import PAT_NAME_LENGTH
from druks.accounts.dependencies import (
    current_account,
    current_account_or_setup,
    current_session_account,
)
from druks.accounts.models import Account, PersonalAccessToken
from druks.accounts.schemas import AccountResponse, IdentityResponse, PatResponse
from druks.api.dependencies import SessionDep
from druks.secrets.models import VaultSecret

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.get("/me", response_model=IdentityResponse, response_model_by_alias=True)
async def get_identity(
    session: SessionDep,
    request: Request,
    account: Account | None = Depends(current_account_or_setup),
) -> IdentityResponse:
    return IdentityResponse(
        auth_mode=request.app.state.settings.identity.mode,
        account=AccountResponse.model_validate(account) if account else None,
        # none/zero has no account before onboarding. A provider revocation still
        # counts, so the operator can reach Reconnect.
        onboarding_required=not (
            account
            and (
                await VaultSecret.list_subscriptions(
                    session, account_id=account.id, include_revoked=True
                )
                or await VaultSecret.list_keys(session)
            )
        ),
    )


@router.get(
    "/accounts",
    response_model=list[AccountResponse],
    response_model_by_alias=True,
    dependencies=[Depends(current_account)],
)
async def list_accounts(session: SessionDep) -> list[Account]:
    return await Account.list_all(session)


@router.get("/personal-tokens", response_model=list[PatResponse], response_model_by_alias=True)
async def list_pats(
    session: SessionDep, account: Account = Depends(current_session_account)
) -> list[PersonalAccessToken]:
    return await PersonalAccessToken.list_for_account(session, account.id)


@router.post("/personal-tokens")
async def create_pat(
    session: SessionDep,
    account: Account = Depends(current_session_account),
    name: str = Body(..., embed=True),
) -> dict[str, str]:
    name = name.strip()
    if name and len(name) <= PAT_NAME_LENGTH:
        # The only time the plaintext leaves Druks. Druks stores its hash.
        _, token = await PersonalAccessToken.create(session, account_id=account.id, name=name)
        return {"token": token}
    raise HTTPException(
        status_code=422,
        detail=f"A token needs a name of at most {PAT_NAME_LENGTH} characters.",
    )


@router.delete(
    "/personal-tokens/{pat_id}", response_model=PatResponse, response_model_by_alias=True
)
async def revoke_pat(
    pat_id: str, session: SessionDep, account: Account = Depends(current_session_account)
) -> PersonalAccessToken:
    pat = await session.get(PersonalAccessToken, pat_id)
    if pat and pat.account_id == account.id:
        await pat.revoke(session)
        return pat
    # A foreign token gets the same 404 as a missing one.
    raise HTTPException(status_code=404, detail="No such token.")
