from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Self

# Execution-side types live with the executor; re-exported here
# because the harness API speaks them.
from druks.sandbox.datastructures import (  # noqa: F401
    AgentInvocation,
    HarnessRunResult,
)
from druks.settings import Settings


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
    harness: str
    # "refreshed" | "fresh" | "busy" | "locked" | "no_refresh_token" | "failed"
    action: str
    error: str | None = None
    expires_at: datetime | None = None
    connection_id: str | None = None


@dataclass(frozen=True)
class ParsedMetric:
    percent_left: int | None
    resets_at: datetime | None
    # Set when a plan meters this model separately; None covers them all.
    model: str | None = None

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
class ParsedUsage:
    ok: bool
    error: str | None = None
    plan_tier: str | None = None
    five_hour: ParsedMetric | None = None
    week: ParsedMetric | None = None
    # Unmetered plan (e.g. Codex business with unlimited credits). The
    # windows above are synthesized permanently-full buckets; consumers
    # should render "unmetered" rather than a quota that never moves.
    unlimited: bool = False
    raw: str = field(default="", repr=False)


@dataclass(frozen=True)
class ParsedModels:
    ok: bool
    error: str | None = None
    # ``{"id", "label"}`` each; codex adds ``"efforts"`` and
    # ``"minimal_client_version"``.
    models: tuple[dict, ...] = ()
    raw: str = field(default="", repr=False)


@dataclass(frozen=True)
class SandboxSettings:
    service_url: str
    service_token: str
    service_timeout: float
    image: str
    # Local CLI config dirs the push anchors config/plugin/skills paths on; the
    # OAuth credential itself is synthesized from the DB at push time, never
    # read from under these. None — no local config for that CLI on this host —
    # carries nothing, so a CLI's local config never sprays into a sandbox
    # uninvited.
    claude_config_dir: Path | None
    codex_config_dir: Path | None
    # Canonical shared-skills dir pushed into both ~/.claude/skills and
    # ~/.codex/skills in the VM. ``None`` => per-CLI fallback (the skills
    # subdir of each home).
    skills_dir: Path | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        return cls(
            service_url=settings.sandbox.service_url,
            service_token=settings.sandbox.service_token,
            service_timeout=settings.sandbox.timeout,
            image=settings.sandbox.image,
            claude_config_dir=settings.claude_config_dir,
            codex_config_dir=settings.codex_config_dir,
            skills_dir=settings.skills_dir,
        )

    @classmethod
    def maybe_from_settings(cls, settings: Settings) -> Self | None:
        if not settings.sandbox.service_url:
            return None
        return cls.from_settings(settings)
