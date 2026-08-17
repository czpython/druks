from datetime import datetime

from pydantic import ConfigDict

from druks.browser.enums import BrowserSessionPayloadFormat, BrowserSessionStatus
from druks.schemas import BaseResponse


class BrowserSessionResponse(BaseResponse):
    model_config = ConfigDict(from_attributes=True)

    name: str
    status: BrowserSessionStatus
    payload_format: BrowserSessionPayloadFormat | None = None
    site: str
    # Only a declaration can vouch for a session; a bare row is a leftover.
    is_declared: bool = False
    created_at: datetime | None = None
    last_refreshed_at: datetime | None = None
    last_used_at: datetime | None = None
