from fastapi import APIRouter, Body, Depends, HTTPException

from druks.accounts.constants import PAT_NAME_LENGTH
from druks.accounts.dependencies import (
    current_account,
    current_account_or_setup,
    current_admin_account,
    current_session_account,
)
from druks.accounts.enums import AccountKind, Role
from druks.accounts.exceptions import LastAdminError
from druks.accounts.models import Account, PersonalAccessToken
from druks.accounts.schemas import AccountResponse, IdentityResponse, PatResponse
from druks.api.dependencies import SessionDep
from druks.secrets.models import VaultSecret
from druks.settings import get_settings

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.get("/me", response_model=IdentityResponse, response_model_by_alias=True)
async def get_identity(
    session: SessionDep,
    account: Account | None = Depends(current_account_or_setup),
) -> IdentityResponse:
    return IdentityResponse(
        auth_mode=get_settings().identity.mode,
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
    return await Account.list_operators(session)


@router.patch(
    "/accounts/{account_id}",
    response_model=AccountResponse,
    response_model_by_alias=True,
    dependencies=[Depends(current_admin_account)],
)
async def set_account_role(
    account_id: str,
    session: SessionDep,
    role: Role = Body(..., embed=True),
) -> Account:
    account = await session.get(Account, account_id)
    if account and account.kind == AccountKind.OPERATOR:
        try:
            await account.set_role(role)
        except LastAdminError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return account
    raise HTTPException(status_code=404, detail="No such operator account.")


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
        await pat.revoke()
        return pat
    # A foreign token gets the same 404 as a missing one.
    raise HTTPException(status_code=404, detail="No such token.")
