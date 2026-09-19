import asyncio
import json
import logging
from contextlib import suppress
from datetime import timedelta
from urllib.parse import urlsplit

import asyncssh
from dbos import DBOS, StepOptions
from pydantic_core import to_json
from sqlalchemy.ext.asyncio import AsyncSession

from druks.accounts.enums import AccountKind
from druks.apps.loader import get_app
from druks.apps.registry import services
from druks.durable.engine import step_session
from druks.durable.models import Run
from druks.files.datastructures import File
from druks.files.storage import get_file_storage
from druks.harnesses.claude import ClaudeHarness
from druks.harnesses.config import AgentConfig, get_config
from druks.locks import lock
from druks.mcp.enums import AllowedTools, Toolkit
from druks.mcp.helpers import get_bearer_token_env_var
from druks.mcp.inbound import get_druks_account_token, get_druks_mcp_server
from druks.mcp.server import get_tool_name
from druks.models import Base
from druks.prompts import render_prompt
from druks.redis import get_client
from druks.sandbox.client import sandbox_client
from druks.sandbox.constants import SANDBOX_HOST_LEASE_SECONDS
from druks.sandbox.exceptions import HostGone
from druks.sandbox.host import Host
from druks.sandbox.layout import get_work_root
from druks.sandbox.models import SandboxIdentity, SecretRef
from druks.sandbox.templates import get_template_id
from druks.workspaces import Workspace

from .bots.constants import ADMIN_PROMPT, ADMIN_TOOLS
from .bridge import Bridge
from .constants import CHAT_KEY_NAME, CONVERSATION_HEADER, FAILURE_MESSAGE, RESULT_MESSAGE
from .enums import MessageRole, MessageState
from .exceptions import ChatBridgeError, ChatHarnessError, ChatSandboxGone
from .models import Conversation, Message
from .sandbox import CHAT_SANDBOX

logger = logging.getLogger(__name__)


def events_key(conversation_id: str) -> str:
    return f"chat:{conversation_id}:events"


async def publish(conversation_id: str, event: dict) -> None:
    key = events_key(conversation_id)
    async with get_client().pipeline(transaction=True) as transaction:
        transaction.xadd(key, {"event": json.dumps(event)})
        transaction.expire(key, SANDBOX_HOST_LEASE_SECONDS)
        await transaction.execute()


async def get_agent(
    session: AsyncSession, conversation: Conversation
) -> tuple[AgentConfig, str, AllowedTools]:
    """How the conversation's agent runs: its settings, its system prompt, and the
    tools its key allows."""
    kind = conversation.account.kind
    app = conversation.connection.identity.get("app", "chat") if conversation.connection else "chat"
    bot = get_app(app).bot
    config = await get_config(session, bot.id, conversation.account_id)
    if kind == AccountKind.OPERATOR:
        return config, await render_prompt(bot.prompt, source=conversation.source), Toolkit.ALL
    if kind == AccountKind.BOT:
        tools = tuple(get_tool_name(name, [bot.app], {bot.app}) for name in bot.user_tools)
        return config, await render_prompt(bot.prompt, source=conversation.source), tools
    tools = tuple(get_tool_name(name, [bot.app], {bot.app}) for name in bot.admin_tools)
    return config, await render_prompt(ADMIN_PROMPT), (*tools, *ADMIN_TOOLS)


async def get_sandbox(
    session: AsyncSession,
    account_id: str,
    config: AgentConfig,
    allowed_tools: AllowedTools,
) -> tuple[Host, SandboxIdentity]:
    """The account's sandbox for a new turn: a live sandbox that holds the Chat
    agent's current secrets, or a new one."""
    server = get_druks_mcp_server(allowed_tools=())
    token = await get_druks_account_token(session, account_id, allowed_tools, name=CHAT_KEY_NAME)
    refs = [
        *config.secret_refs,
        SecretRef(
            name=get_bearer_token_env_var(server.name).lower(),
            secret_id=token.id,
            host=urlsplit(server.url).hostname,
        ),
    ]
    await session.commit()
    async with lock(f"chat:account:{account_id}"):
        identity = await SandboxIdentity.lookup(
            session, account_id=account_id, run_id=None, scoped_to="chat", secret_refs=refs
        )
        if identity:
            with suppress(ChatSandboxGone):
                return await attach_sandbox(session, identity), identity
        # One sandbox per account: a sandbox with other secrets gives way to the new one.
        if previous := await SandboxIdentity.lookup(
            session, account_id=account_id, run_id=None, scoped_to="chat"
        ):
            await sandbox_client.release(host_id=previous.host_id)
        template = await get_template_id(CHAT_SANDBOX)
        identity, entries = await SandboxIdentity.create(
            session, account_id=account_id, run_id=None, scoped_to="chat", secret_refs=refs
        )
        host = await sandbox_client.provision(
            idempotency_key=f"chat:{account_id}:{identity.id}",
            secrets={**config.secrets, **entries},
            template=template,
            identity=identity,
        )
        await Bridge(host).start()
        return host, identity


async def get_running_sandbox(session: AsyncSession, account_id: str) -> Host:
    """The account's live sandbox, whatever its secrets: the sandbox a sent turn runs in."""
    identity = await SandboxIdentity.lookup(
        session, account_id=account_id, run_id=None, scoped_to="chat"
    )
    if identity:
        return await attach_sandbox(session, identity)
    raise ChatSandboxGone("The account has no live Chat sandbox.")


async def attach_sandbox(session: AsyncSession, identity: SandboxIdentity) -> Host:
    await session.commit()
    try:
        async with sandbox_client.attach(host_id=identity.host_id) as host:
            await Bridge(host).start()
        return host
    except HostGone as error:
        await identity.revoke()
        raise ChatSandboxGone("The account's Chat sandbox is gone.") from error


async def reset_live_stream(conversation_id: str) -> None:
    """Drop the live events; open pages reload the saved messages."""
    await get_client().delete(events_key(conversation_id))
    await publish(conversation_id, {"type": "messages"})


@DBOS.workflow(name="chat.deliver")
async def deliver(conversation_id: str) -> None:
    async def deliver_turns() -> None:
        async with step_session() as session:
            conversation = await session.get(Conversation, conversation_id)
            await deliver_pending(session, conversation)

    try:
        await DBOS.run_step_async(StepOptions(name="chat.deliver.turns"), deliver_turns)
    except Exception:
        logger.exception("Chat delivery failed for conversation %s", conversation_id)
        detail = "The reply is unavailable. Connect again to retry."
        await publish(conversation_id, {"type": "error", "detail": detail})


async def deliver_pending(session: AsyncSession, conversation: Conversation) -> None:
    await session.commit()
    async with lock(f"chat:{conversation.id}:delivery"):
        while message := await conversation.get_unanswered_message(session):
            if message.state == MessageState.PENDING:
                if await conversation.is_held(session):
                    return
                await reset_live_stream(conversation.id)
                config, prompt, tools = await get_agent(session, conversation)
                if config.harness_class is not ClaudeHarness:
                    raise ChatHarnessError("Chat supports the Claude harness only.")
                host, identity = await get_sandbox(session, conversation.account_id, config, tools)
                try:
                    bridge = Bridge(host)
                    turn = await send_turn(
                        session, conversation, message, bridge, identity, config, prompt
                    )
                    if turn:
                        await follow_turn(session, conversation, turn, bridge)
                finally:
                    await host.aclose()
                continue
            try:
                host = await get_running_sandbox(session, conversation.account_id)
            except ChatSandboxGone:
                await conversation.end_turn(session, MessageState.INTERRUPTED)
                await session.commit()
                await reset_live_stream(conversation.id)
                continue
            # The snapshot this asks for replaces an error a failed delivery left in the stream.
            await publish(conversation.id, {"type": "messages"})
            try:
                await follow_turn(session, conversation, message, Bridge(host))
            finally:
                await host.aclose()


async def send_turn(
    session: AsyncSession,
    conversation: Conversation,
    message: Message,
    bridge: Bridge,
    identity: SandboxIdentity,
    config: AgentConfig,
    prompt: str,
) -> Message | None:
    """Start the agent and send the pending messages.
    Return the turn's message, unless a Stop or pause came first."""
    host = bridge.host
    status = await bridge.request("status", conversationId=conversation.id)
    if status["status"] == "running":
        raise ChatBridgeError("The bridge has a turn that Druks did not expect.")
    archive_path = ""
    if not status["sessionId"] and conversation.session_file:
        archive_path = f"{get_work_root(host.ssh_username)}/chat/{conversation.id}/restore.tar.gz"
        await host.upload_file(
            local=get_file_storage().path(conversation.session_file.id),
            remote=archive_path,
        )
    server = get_druks_mcp_server(allowed_tools=())
    headers = []
    if conversation.connection:
        headers = [{"name": CONVERSATION_HEADER, "value": conversation.id}]
    meta = config.harness_class.get_acp_meta(config.model_id)
    options = meta["claudeCode"]["options"]
    timeout = 0
    if conversation.account.kind == AccountKind.OPERATOR:
        options = {
            **options,
            "systemPrompt": {"type": "preset", "preset": "claude_code", "append": prompt},
        }
    else:
        options = {
            **options,
            "systemPrompt": prompt,
            "tools": [],
            "settingSources": [],
            "strictMcpConfig": True,
        }
        timeout = config.timeout
    meta = {**meta, "claudeCode": {**meta["claudeCode"], "options": options}}
    await bridge.request(
        "start",
        conversationId=conversation.id,
        archivePath=archive_path,
        command=config.harness_class.adapter_command,
        mode=config.harness_class.no_ask_mode,
        sessionFiles=config.harness_class.session_files,
        meta=meta,
        model=config.model_id,
        effort=config.effort,
        fastMode=config.fast_mode,
        mcpUrl=server.url,
        bearerVariable=get_bearer_token_env_var(server.name),
        headers=headers,
    )
    expires_at = Base.utc_now() + timedelta(seconds=SANDBOX_HOST_LEASE_SECONDS)
    await sandbox_client.set_expiry(host_id=host.id, expires_at=expires_at)
    identity.expires_at = expires_at
    messages = [message]
    if conversation.connection:
        # The sandbox can take seconds to start, and a person can take the chat over meanwhile.
        if await conversation.is_held(session):
            return
        messages = await conversation.list_pending_messages(session)
    delivered_messages = [pending for pending in messages if await pending.mark_delivered(session)]
    await session.commit()
    if delivered_messages:
        await bridge.request(
            "prompt",
            conversationId=conversation.id,
            messageId=delivered_messages[-1].id,
            body="\n\n".join(pending.body for pending in delivered_messages),
            timeout=timeout,
        )
        await publish(conversation.id, {"type": "messages"})
        return delivered_messages[-1]
    return


async def follow_turn(
    session: AsyncSession, conversation: Conversation, message: Message, bridge: Bridge
) -> None:
    redis = get_client()
    position_key = f"chat:{conversation.id}:position"
    while True:
        status = await bridge.request("status", conversationId=conversation.id)
        if status["messageId"] != message.id or status["status"] in ("missing", "interrupted"):
            await conversation.end_turn(session, MessageState.INTERRUPTED)
            await session.commit()
            await reset_live_stream(conversation.id)
            return
        saved = await redis.get(position_key)
        position = json.loads(saved) if saved else {}
        after = position["sequence"] if position.get("epoch") == status["epoch"] else 0
        events = await bridge.events(conversation.id, after)
        if events:
            position = {"epoch": status["epoch"], "sequence": events[-1]["sequence"]}
            async with redis.pipeline(transaction=True) as transaction:
                for event in events:
                    transaction.xadd(
                        events_key(conversation.id),
                        {"event": json.dumps({"type": "event", **event})},
                    )
                transaction.expire(events_key(conversation.id), SANDBOX_HOST_LEASE_SECONDS)
                transaction.set(position_key, json.dumps(position), ex=SANDBOX_HOST_LEASE_SECONDS)
                await transaction.execute()
            after = position["sequence"]
        if status["status"] in ("replied", "cancelled") and after >= status["sequence"]:
            await finish_turn(session, conversation, message, bridge, status)
            return
        if not events:
            await asyncio.sleep(0.25)


async def finish_turn(
    session: AsyncSession,
    conversation: Conversation,
    message: Message,
    bridge: Bridge,
    status: dict,
) -> None:
    """Save the reply and the agent's session files, send a channel's reply to its
    person, and name a new web conversation."""
    body, tool_calls = await bridge.reply(conversation.id, message.id)
    state = MessageState(status["status"])
    await conversation.end_turn(session, state)
    reply = await conversation.create_message(
        session, body, role=MessageRole.ASSISTANT, reply_to=message, tool_calls=tool_calls
    )
    if status["archivePath"]:
        session_file = File(path=status["archivePath"])
        await Workspace(host=bridge.host).save_files(session, [session_file], app="chat")
        await conversation.set_session_file(session, session_file)
    await session.commit()
    await reset_live_stream(conversation.id)
    if conversation.connection:
        # A person who took the chat over during the turn answers it instead.
        if state == MessageState.REPLIED and body and not await conversation.is_held(session):
            channel = services.get(conversation.connection.audience_name)
            await channel.send_reply(session, conversation, reply)
    elif not conversation.title:
        await name_conversation(session, conversation, bridge.host, message, body)


async def report_result(session: AsyncSession, run: Run, *, result) -> str | None:
    body = RESULT_MESSAGE.format(run=run.id, result=to_json(result, fallback=str).decode())
    return await report_outcome(session, run, body)


async def report_failure(session: AsyncSession, run: Run, *, failure: str) -> str | None:
    return await report_outcome(session, run, FAILURE_MESSAGE.format(run=run.id, failure=failure))


async def report_outcome(session: AsyncSession, run: Run, body: str) -> str | None:
    """Report how a run ended to the chat that started it, once the run waited for an
    answer. Returns that conversation."""
    if run.input_requested_at:
        conversation = await session.get(Conversation, run.conversation_id)
        await conversation.create_message(session, body, is_internal=True)
        return conversation.id
    return


async def name_conversation(
    session: AsyncSession, conversation: Conversation, host: Host, message: Message, reply: str
) -> None:
    """Ask Claude for a short name. A failed call leaves the conversation unnamed, and
    the next reply asks again: a name is never worth failing a delivery."""
    prompt = (
        "Name this conversation in at most six words. Reply with the name only.\n\n"
        f"Message: {message.body[:2000]}\n\nReply: {reply[:2000]}"
    )
    with suppress(asyncssh.Error, OSError):
        result = await host.exec([ClaudeHarness.command, "-p", prompt], timeout=30)
        if result.ok and result.stdout.strip():
            conversation.title = result.stdout.strip()[:80]
            await session.commit()
            await publish(conversation.id, {"type": "messages"})


async def cancel_turn(session: AsyncSession, conversation: Conversation, message: Message) -> None:
    is_cancelled = await message.cancel_pending(session)
    await session.commit()
    if is_cancelled:
        await publish(conversation.id, {"type": "messages"})
    else:
        await session.refresh(message)
        await session.commit()
        if message.state == MessageState.DELIVERED:
            host = await get_running_sandbox(session, conversation.account_id)
            try:
                await Bridge(host).request(
                    "cancel", conversationId=conversation.id, messageId=message.id
                )
            finally:
                await host.aclose()
