from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from druks.browser.constants import (
    BROWSER_SESSION_NAME_MAX_LENGTH,
    BROWSER_SESSION_NAME_PATTERN,
    SITE_MAX_LENGTH,
)
from druks.browser.enums import BrowserSessionPayloadFormat, BrowserSessionStatus
from druks.schemas import BaseResponse


class CreateBrowserSessionRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: str = Field(
        min_length=1,
        max_length=BROWSER_SESSION_NAME_MAX_LENGTH,
        pattern=BROWSER_SESSION_NAME_PATTERN,
    )
    payload_format: BrowserSessionPayloadFormat = Field(
        default=BrowserSessionPayloadFormat.STORAGE_STATE,
        alias="payloadFormat",
    )
    site: str = Field(min_length=1, max_length=SITE_MAX_LENGTH)


class BrowserSessionResponse(BaseResponse):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    status: BrowserSessionStatus
    payload_format: BrowserSessionPayloadFormat
    site: str
    created_at: datetime
    last_refreshed_at: datetime | None
    last_used_at: datetime | None
