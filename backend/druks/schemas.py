from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class BaseResponse(BaseModel):
    # Read-side response base: snake_case fields serialize to camelCase for the
    # frontend, so a shape doesn't hand-write serialization_alias on every field.
    # Output-only — request bodies use alias= for input and don't inherit this.
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


__all__ = ["BaseResponse"]
