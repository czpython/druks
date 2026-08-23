from typing import Any, TypeVar

import yaml
from pydantic import BaseModel, ValidationError

from .exceptions import AppConfigError
from .fetcher import fetch_file

ModelT = TypeVar("ModelT", bound=BaseModel)


async def resolve_app_config(app: str, *, repo: str | None, model: type[ModelT]) -> ModelT:
    """Load ``.druks/<app>/config.yml`` from the target repo, validated
    against the model."""
    merged: dict[str, Any] = {}
    if repo and (raw := await fetch_file(repo=repo, path=f".druks/{app}/config.yml")):
        merged.update(yaml.safe_load(raw) or {})
    try:
        return model.model_validate(merged)
    except ValidationError as error:
        raise AppConfigError(f"invalid {app} config for {repo}: {error}") from error
