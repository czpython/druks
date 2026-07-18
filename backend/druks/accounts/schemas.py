from pydantic import ConfigDict

from druks.schemas import BaseResponse


class AccountResponse(BaseResponse):
    model_config = ConfigDict(from_attributes=True)

    id: str
    username: str
