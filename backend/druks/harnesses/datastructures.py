import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import ClassVar, Literal, Self

# Execution-side types live with the executor; re-exported here
# because the harness API speaks them.
from druks.sandbox.datastructures import (  # noqa: F401
    AgentInvocation,
    HarnessRunResult,
)

Billing = Literal["subscription", "api_key"]


@dataclass(frozen=True)
class Manifest:
    """What one call was handed. The harness builds the invocation from it, and
    the call dir keeps it as manifest.json. Presence only: a token records as a
    boolean, never its value, so nothing here needs scrubbing."""

    # Bump when the recorded shape changes so a reader can tell manifests apart
    # across versions; the value is part of the hash, so a bump reshuffles the
    # buckets.
    schema_version: ClassVar[int] = 2

    model: str
    harness: str
    # One entry per enabled registry server and per delivered server: name, url,
    # bearer_token_env_var, declared, delivered, and token_present.
    mcp_servers: tuple[dict, ...]
    # The delivered skills, by name: what the box receives.
    skills: tuple[str, ...]

    @property
    def hash(self) -> str:
        # A stable digest of the canonicalised record, so an identical capability
        # set always hashes the same and an eval report can bucket calls by it.
        canonical = json.dumps(self._capability(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def as_dict(self) -> dict:
        return {"manifest_hash": self.hash, **self._capability()}

    def _capability(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "model": self.model,
            "harness": self.harness,
            "mcp_servers": list(self.mcp_servers),
            "skills_delivered": list(self.skills),
        }


@dataclass(frozen=True)
class OAuthToken:
    """Claude subscription token (read from .credentials.json)."""

    access_token: str
    expires_at: datetime | None
    scopes: tuple[str, ...]
    subscription_type: str | None


@dataclass(frozen=True)
class CodexToken:
    """Codex (ChatGPT) subscription token (read from auth.json)."""

    access_token: str
    expires_at: datetime | None
    account_id: str | None


@dataclass(frozen=True)
class CompletedConnect:
    payload: dict
    provider_email: str
    expires_at: datetime | None
    # The operator account the flow was bound to at start; None on an unbound
    # none/zero setup flow.
    account_id: str | None


@dataclass(frozen=True)
class RotationResult:
    provider: str
    # "refreshed" | "fresh" | "busy" | "locked" | "no_refresh_token" | "failed"
    action: str
    error: str | None = None
    expires_at: datetime | None = None
    subscription_id: str | None = None


@dataclass(frozen=True)
class ParsedMetric:
    percent_left: int | None
    resets_at: datetime | None
    # Set when a plan meters this model separately; None covers them all.
    model: str | None = None
    is_reserve: bool = False

    @classmethod
    def binding(cls, windows: Sequence[Self]) -> Self | None:
        """The window closest to exhaustion — whichever stops work first."""
        binding = None
        remaining = 0
        for window in windows:
            if window.percent_left is None:
                continue
            if binding is None or window.percent_left < remaining:
                binding, remaining = window, window.percent_left
        return binding


@dataclass(frozen=True)
class ProviderRequest:
    """One authenticated GET the platform makes to the provider."""

    url: str
    headers: dict


@dataclass(frozen=True)
class ParsedUsage:
    ok: bool
    main_limit_reached: bool | None = None
    error: str | None = None
    plan_tier: str | None = None
    five_hour: ParsedMetric | None = None
    weeks: tuple[ParsedMetric, ...] = ()
    # Unmetered plan (e.g. Codex business with unlimited credits). The
    # windows above are synthesized permanently-full buckets; consumers
    # should render "unmetered" rather than a quota that never moves.
    unlimited: bool = False
    raw: str = field(default="", repr=False)
