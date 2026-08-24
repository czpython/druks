import importlib
import pkgutil
from collections.abc import Callable
from types import ModuleType
from typing import Any

# Leaf-module names that carry self-registering capabilities. ``autodiscover``
# imports exactly these (``routes`` defines routers the loader mounts; the rest
# fire registration as an import side effect). The set is the single source of
# truth for what "a capability module" is named.
_ROLES = frozenset({"webhooks", "subscribers", "workflows", "tasks", "routes", "services"})


class Registry:
    """A named collection of self-registering capabilities, keyed by a string
    derived from each item. Re-registering the *same* item is idempotent (a
    re-imported module); a *different* item on an existing key is a collision and
    raises — two capabilities can't share a durable identity. ``all()`` returns
    the items in a stable order; ``get()`` resolves one by key."""

    def __init__(self, name: str, *, key: Callable[[Any], str]) -> None:
        self.name = name
        self._key = key
        self._items: dict[str, Any] = {}

    def register(self, item: Any) -> Any:
        key = self._key(item)
        existing = self._items.get(key)
        if existing and existing is not item:
            raise ValueError(
                f"{self.name} registry already has key {key!r} — two capabilities "
                "can't share a durable identity; namespace or rename one"
            )
        self._items[key] = item
        return item

    def get(self, key: str) -> Any:
        return self._items.get(key)

    def all(self) -> list[Any]:
        return [self._items[key] for key in sorted(self._items)]

    def __contains__(self, key: str) -> bool:
        return key in self._items

    def __len__(self) -> int:
        return len(self._items)


def autodiscover(package: str) -> list[ModuleType]:
    """Import every role-named leaf module under ``package`` so the capability
    each one defines self-registers. Returns the imported modules so the loader
    can read routers off the ``routes`` ones; everything else works purely
    through the import side effect."""
    pkg = importlib.import_module(package)
    modules: list[ModuleType] = []
    for info in pkgutil.walk_packages(pkg.__path__, prefix=f"{package}."):
        if info.ispkg or info.name.split(".")[-1] not in _ROLES:
            continue
        modules.append(importlib.import_module(info.name))
    return modules


webhooks = Registry("webhooks", key=lambda cls: f"{cls.__module__}.{cls.__qualname__}")
services = Registry("services", key=lambda cls: cls.slug)
workflows = Registry("workflows", key=lambda cls: cls.kind)
agents = Registry("agents", key=lambda agent: agent.id)
browser_sessions = Registry("browser_sessions", key=lambda session: session.name)
# MCP server definitions from the deployment's catalog, mounted by an explicit
# startup load (druks/mcp/catalog.py); an operator's DB overlay enables and
# tokens them. Dict items, not self-registering classes like the registries above.
mcp_servers = Registry("mcp_servers", key=lambda server: server["name"])
