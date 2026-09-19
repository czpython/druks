import json
import secrets

from dbos import DBOS, Queue, SetEnqueueOptions, SetWorkflowAttributes, SetWorkflowID, StepOptions
from pydantic_core import to_json
from sqlalchemy.ext.asyncio import AsyncSession

from druks.accounts.enums import AccountKind
from druks.apps.loader import get_app
from druks.chat.enums import BotAccess, PauseSignal
from druks.chat.models import Conversation
from druks.chat.service import deliver
from druks.durable.engine import step_session
from druks.durable.models import Run
from druks.redis import get_client
from druks.secrets.enums import SecretKind
from druks.secrets.models import VaultSecret

from .constants import (
    ADMIN_ADDED_MESSAGE,
    ADMIN_CODE_TTL_SECONDS,
    PAUSE_SECONDS,
    PAUSE_TOPIC,
    PAUSED_MESSAGE,
    PHONE_CONNECTED_MESSAGE,
    PHONE_MESSAGE,
    QUESTION_MESSAGE,
)

# A pause holds a chat for hours, so it has its own queue, apart from runs.
pause_queue = Queue("druks_chat_pauses")


async def get_bot_connection(session: AsyncSession, connection_id: str) -> VaultSecret | None:
    """A live connection owned by a bot account."""
    connection = await session.get(VaultSecret, connection_id)
    if (
        connection
        and connection.kind == SecretKind.SESSION
        and connection.is_live
        and connection.account.kind == AccountKind.BOT
    ):
        return connection
    return


async def open_admin_code(connection: VaultSecret, account_id: str) -> str:
    """A one-time phone proof code, owned by the signed-in operator."""
    code = f"{secrets.randbelow(10**8):08d}"
    await get_client().set(
        f"chat:{connection.id}:admin-code",
        json.dumps({"code": code, "account_id": account_id}),
        ex=ADMIN_CODE_TTL_SECONDS,
    )
    return code


async def close_admin_code(connection: VaultSecret, text: str) -> str | None:
    """Consume a matching code and return its operator account id."""
    key = f"chat:{connection.id}:admin-code"
    if value := await get_client().get(key):
        proof = json.loads(value)
        if text.strip() == proof["code"] and await get_client().delete(key):
            return proof["account_id"]


async def add_admin(session: AsyncSession, connection: VaultSecret, user: dict) -> Conversation:
    """Send the number's questions to this person from now on, and greet them through
    the admin's agent. The person shares the admin account with the number's phone."""
    admin = {
        "account_id": connection.identity["admin"]["account_id"],
        "user_id": user["user_id"],
        "name": user["user_name"],
    }
    connection.identity = {**connection.identity, "admin": admin}
    conversation = await Conversation.get_or_create_for_user(
        session, connection, admin["account_id"], **user
    )
    await conversation.create_message(session, ADMIN_ADDED_MESSAGE, is_internal=True)
    return conversation


async def get_or_create_admin_conversation(
    session: AsyncSession, conversation: Conversation
) -> Conversation:
    """Where Druks writes to the admin of the conversation's number: the chat of the
    person who sent the admin code, or else the phone's chat with itself."""
    identity = conversation.connection.identity
    return await Conversation.get_or_create_for_user(
        session,
        conversation.connection,
        identity["admin"]["account_id"],
        source=conversation.source,
        user_id=identity["admin"].get("user_id") or identity["user_id"],
        user_name="",
        user_phone="",
    )


async def route_message(
    session: AsyncSession,
    connection: VaultSecret,
    user: dict,
    *,
    body: str,
    key: str,
    is_from_phone: bool,
    is_self_chat: bool,
) -> Conversation | None:
    """The conversation that a channel's message goes to, or none when the message is
    for no agent. The only text Druks reads is an open admin code."""
    owner = connection.account
    if owner.kind == AccountKind.OPERATOR:
        # Only an operator's chat with themself reaches their agent.
        if is_from_phone and is_self_chat:
            return await Conversation.get_or_create_for_user(session, connection, owner.id, **user)
        return
    bot = get_app(connection.identity["app"]).bot
    if bot.access == BotAccess.PAIRED:
        if is_from_phone:
            return
        if account_id := await close_admin_code(connection, body):
            connection.identity = {
                **connection.identity,
                "operators": {**connection.identity["operators"], user["user_id"]: account_id},
            }
            conversation = await Conversation.get_or_create_for_user(
                session, connection, account_id, **user
            )
            await conversation.create_message(session, PHONE_CONNECTED_MESSAGE, is_internal=True)
            await session.commit()
            await DBOS.start_workflow_async(deliver, conversation.id)
            return
        if account_id := connection.identity["operators"].get(user["user_id"]):
            return await Conversation.get_or_create_for_user(
                session, connection, account_id, **user
            )
        return
    admin = connection.identity["admin"]
    if is_from_phone:
        if is_self_chat:
            # The phone's chat with itself is an admin chat.
            return await Conversation.get_or_create_for_user(
                session, connection, admin["account_id"], **user
            )
        # What the phone sends to the admin person is no chat to take over.
        if user["user_id"] != admin.get("user_id"):
            await take_over(session, connection, user, body, key)
        return
    if await close_admin_code(connection, body):
        conversation = await add_admin(session, connection, user)
        await session.commit()
        await DBOS.start_workflow_async(deliver, conversation.id)
        return
    # The person who sent the admin code talks under the admin account, and everyone
    # else under the bot account.
    account_id = connection.account_id
    if user["user_id"] == admin.get("user_id"):
        account_id = admin["account_id"]
    return await Conversation.get_or_create_for_user(session, connection, account_id, **user)


async def take_over(
    session: AsyncSession, connection: VaultSecret, user: dict, body: str, key: str
) -> None:
    """Keep what the number's phone sent for the agent, and pause the chat. Each
    message from the phone restarts the pause's clock."""
    conversation = await Conversation.get_or_create_for_user(
        session, connection, connection.account_id, **user
    )
    body = PHONE_MESSAGE.format(body=body)
    await conversation.create_message(session, body, is_internal=True, source_id=key)
    await session.commit()
    if pause_id := await conversation.get_pause_id(session):
        await DBOS.send_async(pause_id, PauseSignal.EXTEND, topic=PAUSE_TOPIC)
    else:
        # The typed message names the pause, and the conversation holds one open pause.
        with (
            SetWorkflowID(key),
            SetWorkflowAttributes({"paused_conversation_id": conversation.id}),
            SetEnqueueOptions(
                deduplication_id=f"chat.pause:{conversation.id}",
                duplication_policy="return-existing",
            ),
        ):
            await pause_queue.enqueue_async(pause, conversation.id)


@DBOS.workflow(name="chat.pause")
async def pause(conversation_id: str) -> None:
    """Hold the chat's turns while a person answers it from the number's phone. The
    pause ends when the phone stays quiet for a time, or when the admin resumes the chat."""
    admin_id = await DBOS.run_step_async(
        StepOptions(name="chat.pause.report"), report_pause, conversation_id
    )
    await DBOS.start_workflow_async(deliver, admin_id)
    while await DBOS.recv_async(PAUSE_TOPIC, timeout_seconds=PAUSE_SECONDS) == PauseSignal.EXTEND:
        continue


async def report_pause(conversation_id: str) -> str:
    """Report the pause to the number's admin. Returns the admin's conversation."""
    async with step_session() as session:
        conversation = await session.get(Conversation, conversation_id)
        admin = await get_or_create_admin_conversation(session, conversation)
        body = PAUSED_MESSAGE.format(
            user_name=conversation.user_name,
            user_id=conversation.user_id,
            conversation=conversation.id,
            hours=PAUSE_SECONDS // 3600,
        )
        await admin.create_message(session, body, is_internal=True)
        return admin.id


async def resume(session: AsyncSession, conversation: Conversation) -> bool:
    """End the chat's pause before it ends by itself. Its saved messages go into its
    next turn."""
    if pause_id := await conversation.get_pause_id(session):
        await DBOS.send_async(pause_id, PauseSignal.RESUME, topic=PAUSE_TOPIC)
        return True
    return False


async def ask_admin(session: AsyncSession, run: Run) -> str | None:
    """Ask the number's admin to decide for a parked run that a chat on the number
    started. Returns the admin's conversation."""
    conversation = await session.get(Conversation, run.conversation_id)
    if conversation.admin_account_id and run.input_request:
        admin = await get_or_create_admin_conversation(session, conversation)
        body = QUESTION_MESSAGE.format(
            run=run.id,
            parked_at=run.input_requested_at.isoformat(),
            user_name=conversation.user_name,
            user_id=conversation.user_id,
            request=to_json(await run.get_ask()).decode(),
        )
        await admin.create_message(session, body, is_internal=True)
        return admin.id
    return
