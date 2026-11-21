from typing import Annotated, Any

from dbos import DBOS
from fastapi import APIRouter, Body, Depends, HTTPException, Path, Request

from druks.accounts.context import current_conversation_id
from druks.accounts.dependencies import current_account
from druks.accounts.models import Account
from druks.api.dependencies import SessionDep
from druks.api.schemas import ScheduleResponse
from druks.durable.exceptions import ScheduleError
from druks.durable.models import Schedule
from druks.durable.schedules import apply_schedule

router = APIRouter(prefix="/api/schedules", tags=["schedules"])


@router.post(
    "",
    operation_id="create_schedule",
    status_code=201,
    tags=["agent"],
    response_model=ScheduleResponse,
    response_model_by_alias=True,
)
async def create_schedule(
    request: Request,
    session: SessionDep,
    tool: Annotated[str, Body(description="The name of one of your tools to call.")],
    arguments: Annotated[
        dict[str, Any],
        Body(description="The tool's arguments, the same at every call. Give {} for none."),
    ],
    cron: Annotated[str, Body(description="A cron for when to call, local time.")],
    account: Account = Depends(current_account),
) -> Schedule:
    """Call one of your tools again and again on a cron, as the person in this chat, such
    as a tool that starts a run. When none of your tools does the task, schedule
    chat_run_instruction with the task in words. Each call checks what it checks when
    you make it, and its runs report to this chat. Local time is the installation
    timezone. The answer's nextRunAt is local: tell the person that time as it is. When
    Druks refuses the time, ask the person rather than guess."""
    conversation_id = current_conversation_id.get()
    if not conversation_id:
        raise HTTPException(422, "Make a schedule from a chat. This call comes from no chat.")
    try:
        schedule = await Schedule.create(
            session,
            account_id=account.id,
            conversation_id=conversation_id,
            tool=tool,
            arguments=arguments,
            cron=cron,
            tool_names=request.app.state.agent_tools.values(),
        )
    except ScheduleError as error:
        raise HTTPException(422, str(error)) from error
    await apply_schedule(schedule)
    await session.commit()
    await session.refresh(schedule)
    return schedule


@router.get(
    "",
    operation_id="list_schedules",
    tags=["agent"],
    response_model=list[ScheduleResponse],
    response_model_by_alias=True,
)
async def list_schedules(
    session: SessionDep, account: Account = Depends(current_account)
) -> list[Schedule]:
    """Your schedules, newest first: each one's conversation, tool, arguments, cron, and
    next time."""
    return await Schedule.list_for_account(session, account.id)


@router.put(
    "/{schedule_id}",
    operation_id="change_schedule",
    tags=["agent"],
    response_model=ScheduleResponse,
    response_model_by_alias=True,
)
async def change_schedule(
    request: Request,
    session: SessionDep,
    schedule_id: Annotated[str, Path(description="A schedule from list_schedules.")],
    tool: Annotated[str, Body(description="The name of one of your tools to call.")],
    arguments: Annotated[
        dict[str, Any],
        Body(description="The tool's arguments, the same at every call. Give {} for none."),
    ],
    cron: Annotated[str, Body(description="A cron for when to call, local time.")],
    account: Account = Depends(current_account),
) -> Schedule:
    """Change one of your schedules. Give all three: the tool, its arguments, and the cron
    replace the old ones."""
    schedule = await Schedule.get_for_account(session, schedule_id, account.id)
    if not schedule:
        raise HTTPException(404, "Schedule not found. Choose one from list_schedules.")
    try:
        schedule.change(
            tool=tool,
            arguments=arguments,
            cron=cron,
            tool_names=request.app.state.agent_tools.values(),
        )
    except ScheduleError as error:
        raise HTTPException(422, str(error)) from error
    await apply_schedule(schedule)
    await session.commit()
    await session.refresh(schedule)
    return schedule


@router.delete(
    "/{schedule_id}",
    operation_id="stop_schedule",
    status_code=204,
    tags=["agent"],
)
async def stop_schedule(
    session: SessionDep,
    schedule_id: Annotated[str, Path(description="A schedule from list_schedules.")],
    account: Account = Depends(current_account),
) -> None:
    """Stop one of your schedules. The runs its calls already started go on."""
    schedule = await session.get(Schedule, schedule_id)
    if not schedule:
        raise HTTPException(404, "Schedule not found. Choose one from list_schedules.")
    schedule.check_stopper(account)
    await DBOS.delete_schedule_async(schedule.schedule_name)
    await session.delete(schedule)
    await session.commit()
