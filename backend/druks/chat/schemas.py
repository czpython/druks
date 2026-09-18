from datetime import datetime

from pydantic import ConfigDict

from druks.schemas import Schema
from druks.ui.blocks import FileSummary

from .enums import ConversationSource, MessageRole, MessageState


class ConversationResponse(Schema):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str | None
    source: ConversationSource
    user_id: str | None
    user_name: str
    created_at: datetime
    message_count: int
    active_message_id: str | None


class MessageResponse(Schema):
    model_config = ConfigDict(from_attributes=True)

    id: str
    role: MessageRole
    body: str
    reply_to: str | None
    state: MessageState | None
    tool_calls: list[dict]
    is_internal: bool
    file: FileSummary | None
    created_at: datetime
    delivered_at: datetime | None


class ConversationDetailResponse(ConversationResponse):
    messages: list[MessageResponse]
