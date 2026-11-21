from datetime import datetime
from typing import Any

from dbos import DBOS, StepOptions
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from druks.agents.constants import PRIVATE_AGENT_MESSAGE
from druks.chat.constants import CONVERSATION_HEADER, SKIPPED_MESSAGE
from druks.chat.models import Conversation
from druks.chat.service import deliver, get_agent_tools, report_outcome
from druks.durable.constants import RUN_SCHEDULE_WORKFLOW, SCHEDULE_KEY_NAME
from druks.durable.engine import step_session
from druks.durable.exceptions import ScheduleSkipped
from druks.durable.models import Schedule
from druks.mcp.constants import BEARER_PREFIX
from druks.mcp.exceptions import MissingEndpointError
from druks.mcp.inbound import get_druks_account_token, get_druks_mcp_server
from druks.settings import get_settings


async def apply_schedule(schedule: Schedule) -> None:
    """Make or replace the DBOS schedule that fires the schedule's due times. A time that
    passes while Druks is down does not fire later."""
    await DBOS.apply_schedules_async(
        [
            {
                "schedule_name": schedule.schedule_name,
                "workflow_fn": run_schedule,
                "schedule": schedule.cron,
                "context": {"schedule_id": schedule.id},
                "cron_timezone": get_settings().timezone,
            }
        ]
    )


@DBOS.workflow(name=RUN_SCHEDULE_WORKFLOW)
async def run_schedule(scheduled_at: datetime, context: dict[str, Any]) -> None:
    """Make the schedule's tool call at a due time. When the tool refuses it, the
    conversation hears that Druks skipped the call, and the due time fails."""
    skipped = await DBOS.run_step_async(
        StepOptions(name=f"{RUN_SCHEDULE_WORKFLOW}.call"), call_tool, context["schedule_id"]
    )
    if skipped:
        conversation_id = await DBOS.run_step_async(
            StepOptions(name=f"{RUN_SCHEDULE_WORKFLOW}.report_skip"), report_skip, **skipped
        )
        if conversation_id:
            await DBOS.start_workflow_async(deliver, conversation_id)
        raise ScheduleSkipped(skipped["reason"])


async def call_tool(schedule_id: str) -> dict[str, str] | None:
    """Call the schedule's tool through Druks' own MCP server, as the agent of its
    conversation calls it: with the owner's key for that agent's tools, and from the
    conversation. Returns what to report when the tool refuses the call or Druks cannot
    make it."""
    async with step_session() as session:
        schedule = await session.get(Schedule, schedule_id)
        if not schedule:
            return
        conversation = await session.get(Conversation, schedule.conversation_id)
        # The agent can change owner or turn private after the schedule was made.
        if conversation.agent and not conversation.account.can_talk_to(conversation.agent):
            return {
                "conversation_id": conversation.id,
                "tool": schedule.tool,
                "reason": PRIVATE_AGENT_MESSAGE,
            }
        key = await get_druks_account_token(
            session, schedule.account_id, get_agent_tools(conversation), name=SCHEDULE_KEY_NAME
        )
        bearer = key.secrets["value"].removeprefix(BEARER_PREFIX)
        tool, arguments, conversation_id = (
            schedule.tool,
            schedule.arguments,
            schedule.conversation_id,
        )
    try:
        transport = StreamableHttpTransport(
            get_druks_mcp_server(allowed_tools=()).url,
            headers={CONVERSATION_HEADER: conversation_id},
            auth=bearer,
        )
        async with Client(transport) as client:
            result = await client.call_tool(tool, arguments, raise_on_error=False)
    # fastmcp raises RuntimeError when it cannot reach the server.
    except (MissingEndpointError, RuntimeError) as error:
        reason = f"Druks could not reach its own tools: {error}"
    else:
        if not result.is_error:
            return
        reason = " ".join(block.text for block in result.content if block.type == "text")
    return {"conversation_id": conversation_id, "tool": tool, "reason": reason}


async def report_skip(conversation_id: str, tool: str, reason: str) -> str | None:
    async with step_session() as session:
        return await report_outcome(
            session, conversation_id, SKIPPED_MESSAGE.format(tool=tool, reason=reason)
        )
