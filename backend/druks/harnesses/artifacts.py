import json
import logging
from pathlib import Path
from typing import Any

from .datastructures import Manifest

logger = logging.getLogger(__name__)

_COST_FILENAME = "cost.json"


def write_cost(
    artifact_dir: Path,
    *,
    cost_usd: float | None,
    metadata: dict[str, Any] | None = None,
) -> None:
    if cost_usd is None and not metadata:
        return
    try:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / _COST_FILENAME).write_text(
            json.dumps({"cost_usd": cost_usd, "metadata": metadata or {}}, indent=2),
        )
    except OSError:
        logger.warning("Could not write cost sidecar in %s", artifact_dir, exc_info=True)


def read_cost(artifact_dir: Path) -> tuple[float | None, dict[str, Any] | None]:
    path = artifact_dir / _COST_FILENAME
    if not path.exists():
        return None, None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        logger.warning("Could not read cost sidecar at %s", path, exc_info=True)
        return None, None

    cost_usd = data.get("cost_usd") if isinstance(data, dict) else None
    metadata = data.get("metadata") if isinstance(data, dict) else None
    if cost_usd is not None and not isinstance(cost_usd, int | float):
        cost_usd = None
    if metadata is not None and not isinstance(metadata, dict):
        metadata = None
    return (float(cost_usd) if cost_usd is not None else None), metadata


def normalize_token_usage(metadata: dict[str, Any] | None) -> dict[str, int] | None:
    if not isinstance(metadata, dict):
        return None

    provider = metadata.get("provider")
    if provider == "anthropic":
        raw_input = _int(metadata.get("input_tokens"))
        cache_read = _int(metadata.get("cache_read_input_tokens"))
        cache_write = _int(metadata.get("cache_creation_input_tokens"))
        output = _int(metadata.get("output_tokens"))
        input_total = raw_input + cache_read + cache_write
        cached_input = cache_read
        cache_creation = cache_write
        reasoning = 0
    elif provider == "openai":
        input_total = _int(metadata.get("input_tokens"))
        cached_input = _int(metadata.get("cached_input_tokens"))
        cache_creation = 0
        visible_output = _int(metadata.get("output_tokens"))
        reasoning = _int(metadata.get("reasoning_output_tokens"))
        output = visible_output + reasoning
    else:
        # Unknown provider — try the fields generously so a future
        # adapter that writes the canonical names just works.
        input_total = _int(metadata.get("input_tokens"))
        cached_input = _int(metadata.get("cached_input_tokens"))
        cache_creation = _int(metadata.get("cache_creation_tokens"))
        output = _int(metadata.get("output_tokens"))
        reasoning = _int(metadata.get("reasoning_tokens"))

    if (
        input_total == 0
        and output == 0
        and cached_input == 0
        and cache_creation == 0
        and reasoning == 0
    ):
        return None

    return {
        "input_tokens": input_total,
        "output_tokens": output,
        "cached_input_tokens": cached_input,
        "cache_creation_tokens": cache_creation,
        "reasoning_tokens": reasoning,
        "total_tokens": input_total + output,
    }


def _int(value: Any) -> int:
    if isinstance(value, bool):  # bools are ints in Python; reject explicitly
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return round(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


def call_dir(artifact_dir: Path, call_id: str) -> Path:
    path = artifact_dir / call_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def persist_prompt(artifact_dir: Path, *, call_id: str, prompt: str) -> Path:
    path = call_dir(artifact_dir, call_id) / "prompt.md"
    path.write_text(prompt)
    return path


def persist_manifest(artifact_dir: Path, *, call_id: str, manifest: Manifest) -> Path:
    path = call_dir(artifact_dir, call_id) / "manifest.json"
    path.write_text(json.dumps(manifest.as_dict(), indent=2, sort_keys=True))
    return path
