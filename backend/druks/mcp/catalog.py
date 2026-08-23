import json
from pathlib import Path

from pydantic import ValidationError

from druks.apps.registry import mcp_servers
from druks.mcp.constants import NAME_PATTERN
from druks.mcp.exceptions import InvalidCatalogError
from druks.mcp.schemas import CatalogEntry


def load_mcp_catalog(path: Path) -> None:
    """Register the catalog's server definitions into the ``mcp_servers``
    registry — the explicit startup step (in the app lifespan) that mounts a
    deployment's default servers. The whole file — including collisions with
    already-registered names — validates before the registry is touched, so a
    bad entry never leaves a half-mounted catalog. Entries the registry
    already holds identically (a second app boot in this process) are already
    mounted."""
    try:
        raw = json.loads(path.read_text())
    except OSError as error:
        raise InvalidCatalogError(path, str(error)) from error
    except json.JSONDecodeError as error:
        raise InvalidCatalogError(path, f"not valid JSON ({error})") from error
    if not isinstance(raw, dict):
        raise InvalidCatalogError(path, "top level must be a JSON object of servers by name")
    # The bare name-keyed map is the canonical shape; the "mcpServers" wrapper
    # (Claude/Cursor/Windsurf convention) is tolerated.
    entries = raw.get("mcpServers", raw)
    if not isinstance(entries, dict):
        raise InvalidCatalogError(path, '"mcpServers" must be a JSON object of servers by name')
    definitions = []
    for name, entry in entries.items():
        if not NAME_PATTERN.match(name):
            raise InvalidCatalogError(
                path,
                f"invalid server name {name!r}: use lowercase letters, digits and "
                "underscores, starting with a letter",
            )
        try:
            parsed = CatalogEntry.model_validate(entry)
        except ValidationError as error:
            raise InvalidCatalogError(path, f"server {name!r}: {error}") from error
        # Normalized to the uniform registry item every consumer reads directly.
        definitions.append(
            {
                "name": name,
                "url": parsed.url,
                "token_source": parsed.auth.type,
                "source_env_var": parsed.auth.source_env_var,
                "enabled": parsed.enabled,
            }
        )
    to_register = [d for d in definitions if mcp_servers.get(d["name"]) != d]
    collisions = sorted(d["name"] for d in to_register if d["name"] in mcp_servers)
    if collisions:
        raise InvalidCatalogError(
            path, f"already registered with a different definition: {', '.join(collisions)}"
        )
    for definition in to_register:
        mcp_servers.register(definition)
