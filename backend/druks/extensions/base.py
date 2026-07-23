import importlib.util
import re
from collections.abc import Callable, Sequence
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel

from druks.events.feed import generic_entry
from druks.events.models import Event
from druks.user_settings.models import SettingsOverride

from .registry import agents as agent_registry
from .registry import autodiscover
from .registry import workflows as workflow_registry
from .settings import (
    coerce_setting_value,
    validate_setting_override,
    validate_settings_declaration,
)

if TYPE_CHECKING:
    from fastapi import APIRouter, FastAPI

    from druks.agents import Agent
    from druks.doctor import CheckResult
    from druks.durable.schemas import SubjectActivity, SubjectSummary
    from druks.events.feed import FeedItem
    from druks.settings import Settings
    from druks.workflows import Workflow

    # A check the extension owns: given the resolved settings, it returns a verdict on
    # one of the extension's own preconditions (its API key, a webhook secret, a
    # provider being reachable). Same signature and ``CheckResult`` shape as a core
    # check, so the operator sees one uniform report.
    Check = Callable[[Settings], CheckResult]

# An extension name keys the ``/api/<name>`` namespace, the ``alembic_version_<name>``
# table, the ``<name>_`` table prefix, and ``extension:<name>:`` settings — so it must
# be a lowercase SQL/URL-safe identifier. Public: the scaffolder validates against the
# same rule so ``druks create extension`` can't emit a package that fails this check.
NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class Extension:
    """A pluggable application. Subclass it, set ``name``, and register the
    subclass under the ``druks.extensions`` entry-point group. At boot the platform
    calls ``load`` for every extension, which imports the package's
    conventionally-named modules — that import is where the extension's webhooks,
    workflows, agents, and subscribers self-register.

    Used as a class, never instantiated: an extension is a stateless install
    singleton, so an instance would only be ceremony.
    """

    name: ClassVar[str]
    # Tables this extension owns are prefixed ``<name>_`` — the SQLAlchemy stand-in for a
    # Django ``app_label``, derived from ``name``. The platform scopes the extension's
    # autogenerate to this prefix and tracks its history in ``alembic_version_<name>``.
    table_prefix: ClassVar[str]
    # Whether this extension's tables must carry the ``<name>_`` prefix. True for a
    # normally-shipped extension, so its schema can't collide with core or another
    # extension. An extension whose tables instead live in the platform's own migration
    # history — bundled and predating the prefix convention — sets this False to opt
    # those tables out of the boot-time check.
    prefix_tables: ClassVar[bool] = True
    # The rail glyph, named from the Lucide set the frontend bundles (e.g.
    # "telescope", "hammer" — see the UI's APP_ICONS for the available names). A
    # extension just names one, so a separately-shipped package gets a glyph without
    # touching the frontend; unknown names fall back to the default.
    icon: ClassVar[str] = "box"
    # One-line blurb shown in the settings pane when the extension is selected.
    description: ClassVar[str] = ""
    # The extension's top-level package, walked by ``discover``. Defaults to the
    # package the subclass is defined in — the ``<package>/extension.py`` convention
    # means that's always the extension's root — so it's only set explicitly when the
    # class lives somewhere other than its package root.
    package: ClassVar[str]
    # ``builtin`` extensions carry platform-core settings rather than a user-facing
    # extension — the settings UI folds their agents into the Druks tab instead of
    # giving them their own.
    builtin: ClassVar[bool] = False
    # The extension's declared ``Settings`` inner class, if any — operator knobs that
    # belong to the extension itself rather than one of its workflows. Mirrors a
    # workflow's ``Settings``.
    settings_model: ClassVar[type[BaseModel] | None] = None
    # The kind of subject this extension's runs are about (e.g. "work_item"). Set it to
    # get the generic subject read-side mounted at ``/api/<name>/<subject_type>`` — the
    # platform serves status + timeline + live stream, and the extension supplies only
    # the domain summary. None = the extension has no subject read-side.
    subject_type: ClassVar[str | None] = None
    # The checks this extension contributes to ``druks doctor`` — one per precondition
    # it owns (its API key set, its webhook secret present, its provider reachable).
    # ``druks doctor`` runs each through the same ``CheckResult`` report as its core
    # checks, isolating a raising one under this extension's name so it can't hide a
    # core failure. Default none; declare a list to add them.
    checks: "ClassVar[list[Check]]" = []

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        name = getattr(cls, "name", None)
        if not name:
            raise TypeError(f"{cls.__name__} must set a `name`")
        if not NAME_RE.match(name):
            raise TypeError(
                f"extension name {name!r} must match {NAME_RE.pattern!r} — it keys the "
                "/api/<name> namespace, the version table, and settings keys"
            )
        cls.table_prefix = f"{name}_"
        if "package" not in cls.__dict__:
            cls.package = cls.__module__.rpartition(".")[0]
        declared = cls.__dict__.get("Settings")
        if isinstance(declared, type) and issubclass(declared, BaseModel):
            validate_settings_declaration(declared)
            cls.settings_model = declared

    @classmethod
    def settings(cls) -> BaseModel:
        """The extension's settings, resolved through the override store keyed by extension
        name. Raises if the extension declares no ``Settings``."""
        model = cls.settings_model
        if not model:
            raise TypeError(f"extension {cls.name!r} declares no Settings")
        values = {
            name: SettingsOverride.extension_setting(cls.name, name, field.default)
            for name, field in model.model_fields.items()
        }
        return model.model_validate(values)

    @classmethod
    def override_setting(cls, field: str, value: Any) -> None:
        """An operator's override for one declared setting; ``None`` clears it back
        to the declared default. Raises ``ValueError`` so the API layer can 422 it."""
        model = cls.settings_model
        if not model or field not in model.model_fields:
            raise ValueError(f"Unknown {cls.name} setting {field!r}")
        if value is not None:
            value = coerce_setting_value(model, field, value)
            validate_setting_override(model, cls.settings().model_dump(), field, value)
        SettingsOverride.set_extension_setting(cls.name, field, value)

    @classmethod
    def agents(cls) -> "list[Agent]":
        """The agents declared on this extension class — their ``extension`` field is
        stamped in ``__set_name__``. Registry order is id-sorted."""
        return [agent for agent in agent_registry.all() if agent.extension == cls.name]

    @classmethod
    def workflows(cls) -> "list[type[Workflow]]":
        """The workflows living in this extension's package."""
        prefix = cls.package + "."
        return [wf for wf in workflow_registry.all() if wf.__module__.startswith(prefix)]

    @classmethod
    def discover(cls) -> list[ModuleType]:
        """Import the extension's capability modules so its webhooks, workflows,
        agents, and subscribers self-register. The default walks ``package``;
        override to customize discovery (the Django ``ExtensionConfig.ready`` escape
        hatch — the override, not a platform special-case, is how a weird extension
        stays weird). Returns the imported modules so ``get_routers`` can read the
        routers off the ``routes`` ones."""
        return autodiscover(cls.package)

    @classmethod
    def capability_modules(cls) -> list[ModuleType]:
        """The extension's imported capability modules — the ``routes``,
        ``subscribers``, ``workflows``, and ``webhooks`` leaves ``discover``
        walks. Enumerates the extension's route/subscriber/webhook surface
        app-lessly (each ``@subscribe`` and ``Webhook`` self-registers on
        import, so the modules are the surface); an alias for ``discover`` read
        as a surface rather than a side effect."""
        return cls.discover()

    @classmethod
    def routers(cls) -> "list[APIRouter]":
        """Every router the extension mounts, enumerated without the web app —
        its declared ``routes`` routers plus the free read-sides. Builds the
        ``APIRouter`` objects but constructs no FastAPI app, so a CLI or eval can
        read the extension's route surface without booting the platform."""
        return cls.get_routers(cls.discover())

    @classmethod
    def migrations_dir(cls) -> Path | None:
        """The extension's own migration history root (``<package>/migrations``),
        or None when it ships no migrations — a builtin whose tables live in
        core's schema, or a not-yet-migrated extension. The ``versions/`` dir it
        contains is what ``druks init-db`` upgrades under
        ``alembic_version_<name>``."""
        package_dir = cls.package_dir()
        if not package_dir:
            return None
        migrations = package_dir / "migrations"
        return migrations if (migrations / "versions").is_dir() else None

    @classmethod
    def package_dir(cls) -> Path | None:
        """Filesystem root of ``package`` — where the extension's shipped non-module
        assets (``migrations/``, ``dist/``) live. None when the package has no
        location (a namespace-less or frozen import)."""
        spec = importlib.util.find_spec(cls.package)
        if not spec or not spec.submodule_search_locations:
            return None
        return Path(spec.submodule_search_locations[0])

    @classmethod
    def frontend_dist(cls) -> Path | None:
        """The extension's built frontend (``<package>/dist``), if it ships one.
        Inside the package, not the project root, so the same path resolves for a
        wheel and an editable install alike."""
        package_dir = cls.package_dir()
        if not package_dir:
            return None
        dist = package_dir / "dist"
        return dist if (dist / "index.html").is_file() else None

    @classmethod
    def load(cls, app: "FastAPI") -> None:
        """Wire the extension into the running API: import its capabilities
        (``discover``), mount its routers under ``/api/<name>``, and serve its
        shipped frontend (if any) under ``/app/<name>``. The loader calls this once
        per extension at boot."""
        # Local, matching get_routers: the loader stays importable app-lessly.
        from fastapi import Depends

        from druks.accounts.dependencies import current_account

        modules = cls.discover()
        # /api/<name> wraps the author's own prefix so extensions can't shadow
        # the platform or each other; every route sits behind the identity gate.
        prefix = f"/api/{cls.name}"
        for router in cls.get_routers(modules):
            app.include_router(router, prefix=prefix, dependencies=[Depends(current_account)])
        dist = cls.frontend_dist()
        if dist:
            # /app, not /api: unknown /api/* paths must stay JSON 404s, never fall
            # through to an index.html.
            app.frontend(f"/app/{cls.name}", directory=dist)

    @classmethod
    def get_routers(cls, modules: list[ModuleType]) -> "list[APIRouter]":
        """Every router mounted under the extension's namespace: the ones it declares in
        its ``routes`` modules, plus the generic read-side it gets for free —
        ``/transcripts`` always, and the subject read-side
        (``/<subject_type>`` → status + timeline + live stream) when it declares a
        ``subject_type``. Override to add a router built outside a ``routes`` module."""
        # Local, not module-top: keeps FastAPI off the import graph so the loader
        # stays importable app-lessly; enumerating routers is where it's really needed.
        from fastapi import APIRouter

        seen: set[int] = set()
        declared: list[APIRouter] = []
        for module in modules:
            if module.__name__.rsplit(".", 1)[-1] != "routes":
                continue
            for value in vars(module).values():
                if isinstance(value, APIRouter) and id(value) not in seen:
                    seen.add(id(value))
                    declared.append(value)
        routers = [*declared, cls._get_transcript_routes()]
        if cls.subject_type:
            routers.append(cls._get_subject_routes())
        return routers

    @classmethod
    def _get_transcript_routes(cls) -> "APIRouter":
        """The agent-call read-side every extension gets for free: a paginated read and a
        live tail of a call's stdout/stderr, plus its artifact files (prompt, response,
        transcript streams, metadata) listed and downloadable. Keyed by the platform's own
        ``AgentCall`` and mounted under ``/api/<name>/transcripts`` — an extension writes
        none of it."""
        # Keep FastAPI and the durable read-side off the import graph so the loader
        # stays importable app-lessly.
        import mimetypes
        from typing import Literal

        from fastapi import APIRouter, HTTPException, status
        from fastapi.responses import FileResponse, StreamingResponse

        from druks.api.dependencies import EngineDep
        from druks.durable import reads
        from druks.durable.live import SSE_HEADERS
        from druks.durable.models import AgentCall
        from druks.durable.schemas import AgentCallFiles, TranscriptChunk

        default_limit = 64 * 1024
        max_limit = 256 * 1024

        router = APIRouter(prefix="/transcripts/{call_id}", tags=[f"{cls.name}:transcripts"])

        @router.get("", response_model=TranscriptChunk, response_model_by_alias=True)
        async def get_transcript(
            call_id: str,
            stream: Literal["stdout", "stderr"],
            engine: EngineDep,
            offset: int = 0,
            limit: int = default_limit,
        ) -> TranscriptChunk:
            if offset < 0:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "offset must be >= 0.")
            if not 0 < limit <= max_limit:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST, f"limit must be in 1..{max_limit}."
                )
            chunk = reads.read_transcript_chunk(engine, call_id, stream, offset=offset, limit=limit)
            if not chunk:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found.")
            return chunk

        @router.get("/stream", response_class=StreamingResponse)
        async def stream_transcript(
            call_id: str,
            stream: Literal["stdout", "stderr"],
            engine: EngineDep,
            offset: int = 0,
        ) -> StreamingResponse:
            return StreamingResponse(
                reads.stream_transcript(engine, call_id, stream, offset=offset),
                media_type="text/event-stream",
                headers=SSE_HEADERS,
            )

        @router.get("/files", response_model=AgentCallFiles, response_model_by_alias=True)
        async def list_files(call_id: str) -> AgentCallFiles:
            files = reads.get_agent_call_files(call_id)
            if not files:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent call not found.")
            return files

        @router.get("/files/{file_name:path}")
        async def get_file(
            call_id: str,
            file_name: str,
            disposition: Literal["inline", "attachment"] = "inline",
        ) -> FileResponse:
            call = AgentCall.get(call_id)
            if not call:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent call not found.")
            resolved = call.get_file_path(file_name)
            if not resolved:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found for this call.")
            media_type, _ = mimetypes.guess_type(resolved.name)
            return FileResponse(
                resolved,
                media_type=media_type or "application/octet-stream",
                filename=resolved.name if disposition == "attachment" else None,
            )

        return router

    @classmethod
    def _get_subject_routes(cls) -> "APIRouter":
        """The board and one subject (header + status + timeline + activity), each with a
        point-in-time read and a ``/stream`` that pushes the whole snapshot on change.
        Mounted at ``/api/<name>/<subject_type>`` once the extension sets ``subject_type``."""
        from fastapi import APIRouter, HTTPException, status
        from fastapi.responses import StreamingResponse

        from druks.api.dependencies import EngineDep
        from druks.database import session_scope
        from druks.durable import reads
        from druks.durable.live import SSE_HEADERS, stream
        from druks.durable.schemas import SubjectList, SubjectResponse, SubjectRow

        kind = cls.subject_type
        assert kind is not None

        router = APIRouter(prefix=f"/{kind}", tags=[f"{cls.name}:{kind}"])

        def board() -> SubjectList:
            return SubjectList(
                rows=[
                    SubjectRow(summary=summary, status=reads.get_subject_status(kind, summary.id))
                    for summary in cls.list_subjects()
                ]
            )

        async def subject(subject_id: str) -> SubjectResponse | None:
            summary = cls.subject_summary(subject_id)
            if not summary:
                return None
            activity = await cls.subject_activity(subject_id)
            return reads.get_subject_response(kind, subject_id, summary=summary, activity=activity)

        @router.get("", response_model=SubjectList, response_model_by_alias=True)
        async def list_subjects() -> SubjectList:
            return board()

        # ``/stream`` before ``/{subject_id}`` so the literal path wins over the id matcher.
        @router.get("/stream", response_class=StreamingResponse)
        async def stream_board(engine: EngineDep) -> StreamingResponse:
            async def snapshot() -> SubjectList:
                with session_scope(engine):
                    return board()

            return StreamingResponse(
                stream(snapshot), media_type="text/event-stream", headers=SSE_HEADERS
            )

        @router.get("/{subject_id}", response_model=SubjectResponse, response_model_by_alias=True)
        async def get_subject(subject_id: str) -> SubjectResponse:
            response = await subject(subject_id)
            if not response:
                raise HTTPException(status.HTTP_404_NOT_FOUND, f"No {kind} {subject_id!r}.")
            return response

        @router.get("/{subject_id}/stream", response_class=StreamingResponse)
        async def stream_subject(subject_id: str, engine: EngineDep) -> StreamingResponse:
            async def snapshot() -> SubjectResponse | None:
                with session_scope(engine):
                    return await subject(subject_id)

            return StreamingResponse(
                stream(snapshot), media_type="text/event-stream", headers=SSE_HEADERS
            )

        return router

    @classmethod
    async def on_startup(cls) -> None:
        """Converge runtime state when the API process boots — after extension load
        and DBOS launch. Default no-op; an extension overrides it to sync schedules or
        similar. The caller logs a failure and moves on, so one extension can't wedge
        boot."""

    @classmethod
    def record_event(
        cls,
        *,
        type: str,
        subject: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Record one of this extension's domain events to the log, stamped with the
        extension automatically. Apps record through here so the ``Event`` model
        stays a platform internal — the write-side twin of ``format_event``."""
        Event.emit(type=type, subject=subject, payload=payload, extension=cls.name)

    @classmethod
    def format_event(cls, event: Event) -> "FeedItem":
        """Render one of this extension's events into an activity-feed row. The core
        feed dispatches each event to its extension by ``event.extension``, so the core
        never learns an extension's event types — override to give them human
        summaries."""
        return generic_entry(event)

    @classmethod
    def subject_summary(cls, subject_id: str) -> "SubjectSummary | None":
        """This extension's domain header for one subject — its own fields only; the
        read-side composes it with the platform's generic status + timeline. None when
        the id isn't one of its subjects. Required once ``subject_type`` is set."""
        raise NotImplementedError(
            f"extension {cls.name!r} sets subject_type but no subject_summary"
        )

    @classmethod
    def list_subjects(cls) -> "Sequence[SubjectSummary]":
        """This extension's subjects, newest-movement first, each as its domain summary.
        Returns a covariant ``Sequence`` so an extension can return ``list`` of its own
        ``SubjectSummary`` subclass. Required once ``subject_type`` is set."""
        raise NotImplementedError(f"extension {cls.name!r} sets subject_type but no list_subjects")

    @classmethod
    async def subject_activity(cls, subject_id: str) -> "SubjectActivity | None":
        """The subject's live sub-phase, if any (e.g. "Building sandbox VM…"). Optional —
        override to surface a transient signal the running run pushes."""
        return None
