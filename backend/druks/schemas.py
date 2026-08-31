from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class Schema(BaseModel):
    # What the API answers with. Output only: a request body takes alias= for
    # input and does not inherit this.
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


__all__ = ["Schema"]
