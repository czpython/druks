from datetime import datetime

from pydantic import ConfigDict

from druks.schemas import Schema

from .enums import ConversationSource, MessageRole, MessageState


class ConversationResponse(Schema):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str | None
    source: ConversationSource
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
    created_at: datetime
    delivered_at: datetime | None


class ConversationDetailResponse(ConversationResponse):
    messages: list[MessageResponse]
