import json
from pathlib import Path
from typing import Any

from .exceptions import HarnessInvalidOutputError


def read_result_json(output_path: Path, *, name: str) -> dict[str, Any]:
    """Load + validate the result-JSON file a CLI wrote in the VM.

    The schema is requested via prompt instruction (not a hard CLI
    constraint), so tolerate the one realistic slip — markdown code
    fences around the object. Anything else fails the run loudly."""
    try:
        text = output_path.read_text().strip()
    except FileNotFoundError as error:
        raise HarnessInvalidOutputError(f"{name} did not write result JSON.") from error

    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise HarnessInvalidOutputError(f"{name} wrote invalid result JSON.") from error

    if not isinstance(payload, dict):
        raise HarnessInvalidOutputError(f"{name} result JSON must be an object.")

    return payload
