from typing import Literal

from druks.schemas import Schema


class AdminCodeResponse(Schema):
    code: str
    expires_in: int


class ResumeConversationResponse(Schema):
    conversation: str
    result: Literal["resumed", "not_paused"]
