import importlib.util
import re
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Annotated, Any, ClassVar

from pydantic import BaseModel, Field, SecretStr

from druks.events.models import Event
from druks.models import StoredSubject
from druks.user_settings.models import SettingsOverride

from .exceptions import ExtensionSubjectContractError, SettingsDeclarationError
from .registry import agents as agent_registry
from .registry import autodiscover
from .registry import workflows as workflow_registry
from .settings import (
    coerce_setting_value,
    field_kind,
    validate_setting_override,
    validate_settings_declaration,
)

if TYPE_CHECKING:
    from fastapi import APIRouter

    from druks.agents import Agent
    from druks.doctor import CheckResult
    from druks.durable.datastructures import Subject
    from druks.durable.schemas import SubjectActivity
    from druks.workflows import Workflow

    # A check the extension owns returns a verdict on one of its own preconditions
    # using the same ``CheckResult`` shape as a core check.
    Check = Callable[[], CheckResult]

# An extension name keys the ``/api/<name>`` namespace, the ``alembic_version_<name>``
# table, the ``<name>_`` table prefix, and ``extension:<name>:`` settings — so it must
# be a lowercase SQL/URL-safe identifier. Public: the scaffolder validates against the
# same rule so ``druks create extension`` can't emit a package that fails this check.
NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


# A secret-kind settings field: unset is an empty ``SecretStr`` — falsy, so
# ``if self.token:`` reads set-ness and ``.get_secret_value()`` never needs a guard.
Secret = Annotated[SecretStr, Field(default=SecretStr(""))]


class ExtensionSettings(BaseModel):
    def clean(self) -> dict[str, str]:
        """Problems with the resolved settings, keyed by the field the operator
        must fix. Advisory at rest, blocking on save: the settings form rejects
        a save that would create them; doctor reports the ones already stored."""
        return {}


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
    # The extension's appbar subnav tabs, as (url, name) pairs. The switcher
    # label itself is derived from ``name`` (underscores become spaces), so an
    # extension only declares destinations, never a title.
    navigation: ClassVar[list[tuple[str, str]]] = []
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
    settings_model: ClassVar[type[ExtensionSettings] | None] = None
    # The checks this extension contributes to ``druks doctor`` — one per precondition
    # beyond resolved-settings coherence (for example, whether its provider is reachable).
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
        if "subject_type" in cls.__dict__:
            raise TypeError(
                f"{cls.__name__} declares subject_type — a workflow declares what its runs "
                "are about (``subject = YourSubject``), and an extension's subjects follow "
                "from its workflows"
            )
        cls.table_prefix = f"{name}_"
        if "package" not in cls.__dict__:
            cls.package = cls.__module__.rpartition(".")[0]
        declared = cls.__dict__.get("Settings")
        if declared is not None:
            if not isinstance(declared, type) or not issubclass(declared, ExtensionSettings):
                raise SettingsDeclarationError(
                    f"{cls.__name__}.Settings must subclass ExtensionSettings"
                )
            validate_settings_declaration(declared)
            cls.settings_model = declared

    @classmethod
    def settings(cls) -> ExtensionSettings:
        """The extension's settings, resolved through the override store keyed by extension
        name. Raises if the extension declares no ``Settings``."""
        model = cls.settings_model
        if not model:
            raise TypeError(f"extension {cls.name!r} declares no Settings")
        values = {
            name: SettingsOverride.extension_setting(
                cls.name,
                name,
                field.default,
                is_secret=field_kind(field) == "secret",
            )
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
        SettingsOverride.set_extension_setting(
            cls.name,
            field,
            value,
            is_secret=field_kind(model.model_fields[field]) == "secret",
        )

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
    def subject_classes(cls) -> "list[type[Subject] | type[StoredSubject]]":
        """What this extension's runs are about, read off the workflows that declare
        them — each one gets a board and a page, ordered by subject type so the routes
        it mounts are stable.

        Validates each declared subject against the read-side contract the platform
        will call: the reserved ``transcripts`` segment, and a ``list_summaries()``
        the board invokes. The method's identity is inspected, never called — a
        concrete override (declared or inherited) satisfies the contract without
        running extension query code at load."""
        from druks.durable.datastructures import Subject

        # The platform stubs both bases raise ``NotImplementedError`` from — a subject
        # resolving ``list_summaries`` to one of these has not implemented it.
        stub_impls = {
            Subject.list_summaries.__func__,
            StoredSubject.list_summaries.__func__,
        }
        declared = {wf.subject for wf in cls.workflows() if wf.subject}
        for subject_class in declared:
            if subject_class.subject_type == "transcripts":
                raise ExtensionSubjectContractError(
                    f"{subject_class.__name__} is a 'transcripts' subject; that segment "
                    "serves every extension's agent-call reads. Name it for what it is"
                )
            if subject_class.list_summaries.__func__ in stub_impls:
                raise ExtensionSubjectContractError(
                    f"extension {cls.name!r} declares subject {subject_class.__name__}, "
                    f"whose board calls list_summaries(), but {subject_class.__name__} "
                    f"does not implement it. Implement list_summaries() on "
                    f"{subject_class.__name__}."
                )
        return sorted(declared, key=lambda subject_class: subject_class.subject_type)

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
        A dist is an ESM module the shell mounts, not a document — ``entry.js``
        is its marker. Inside the package, not the project root, so the same
        path resolves for a wheel and an editable install alike."""
        package_dir = cls.package_dir()
        if not package_dir:
            return None
        dist = package_dir / "dist"
        return dist if (dist / "entry.js").is_file() else None

    @classmethod
    def get_routers(cls, modules: list[ModuleType]) -> "list[APIRouter]":
        """Every router mounted under the extension's namespace: the ones it declares in
        its ``routes`` modules, plus the generic read-side it gets for free —
        ``/transcripts`` always, and one subject read-side
        (``/<subject_type>`` → status + timeline + live stream) per subject its
        workflows declare. The platform's come first: those segments are its own.
        Override to add a router built outside a ``routes`` module."""
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
        # The platform's routers are narrow — each confined to its own segment — so
        # matching them first costs an extension nothing anywhere else and leaves it
        # no way to take a read the platform serves, not even with a catch-all.
        return [
            cls._get_transcript_routes(),
            *(cls._get_subject_routes(subject) for subject in cls.subject_classes()),
            *declared,
        ]

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

        from fastapi import APIRouter, HTTPException, Response, status
        from fastapi.responses import FileResponse, StreamingResponse

        from druks.api.dependencies import EngineDep
        from druks.durable import reads
        from druks.durable.enums import AgentCallStatus
        from druks.durable.live import SSE_HEADERS
        from druks.durable.models import AgentCall
        from druks.durable.schemas import AgentCallFiles, TranscriptChunk

        default_limit = 64 * 1024
        max_limit = 256 * 1024
        # A call that has stopped writing never appends to its log again, so the
        # byte range this serves is permanent.
        settled_cache = "public, max-age=31536000, immutable"

        router = APIRouter(prefix="/transcripts/{call_id}", tags=[f"{cls.name}:transcripts"])

        @router.get("", response_model=TranscriptChunk, response_model_by_alias=True)
        async def get_transcript(
            call_id: str,
            stream: Literal["stdout", "stderr"],
            response: Response,
            offset: int = 0,
            limit: int = default_limit,
        ) -> TranscriptChunk:
            if offset < 0:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "offset must be >= 0.")
            if not 0 < limit <= max_limit:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST, f"limit must be in 1..{max_limit}."
                )
            call = AgentCall.get(call_id)
            if call.live_status == AgentCallStatus.RUNNING:
                response.headers["Cache-Control"] = "no-store"
            else:
                response.headers["Cache-Control"] = settled_cache
            return reads.read_transcript_chunk(call, stream, offset=offset, limit=limit)

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
            return reads.get_agent_call_files(call_id)

        @router.get("/files/{file_name:path}")
        async def get_file(
            call_id: str,
            file_name: str,
            disposition: Literal["inline", "attachment"] = "inline",
        ) -> FileResponse:
            call = AgentCall.get(call_id)
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
    def _get_subject_routes(
        cls, subject_class: "type[Subject] | type[StoredSubject]"
    ) -> "APIRouter":
        """The board and one subject (header + status + timeline + activity), each with a
        point-in-time read and a ``/stream`` that pushes the whole snapshot on change.
        Mounted at ``/api/<name>/<subject_type>`` for every subject the extension's
        workflows declare. Every read here is keyed by identity, so an extension that
        keeps no row for its subject gets the same surface as one that does."""
        from fastapi import APIRouter, HTTPException, status
        from fastapi.responses import StreamingResponse

        from druks.api.dependencies import EngineDep
        from druks.database import session_scope
        from druks.durable import reads
        from druks.durable.live import SSE_HEADERS, stream
        from druks.durable.schemas import SubjectList, SubjectResponse, SubjectRow

        subject_type = subject_class.subject_type
        router = APIRouter(prefix=f"/{subject_type}", tags=[f"{cls.name}:{subject_type}"])

        def board() -> SubjectList:
            return SubjectList(
                rows=[
                    SubjectRow(
                        summary=summary,
                        status=reads.get_subject_status(subject_type, summary.id),
                    )
                    for summary in subject_class.list_summaries()
                ]
            )

        async def subject_response(subject_id: str) -> SubjectResponse | None:
            subject = subject_class.get_for_subject_id(subject_id)
            if subject is None:
                return
            return reads.get_subject_response(
                subject_type,
                subject_id,
                summary=subject.get_summary(),
                activity=await cls.get_subject_activity(subject),
            )

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

        # A subject id is whatever identifies it — "7" for a row, "owner/repo#7" for a
        # pull request — so the id matcher spans separators, and the subject's own
        # ``/stream`` is declared ahead of it to keep the greedy match off it.
        @router.get("/{subject_id:path}/stream", response_class=StreamingResponse)
        async def stream_subject(subject_id: str, engine: EngineDep) -> StreamingResponse:
            async def snapshot() -> SubjectResponse | None:
                with session_scope(engine):
                    return await subject_response(subject_id)

            return StreamingResponse(
                stream(snapshot), media_type="text/event-stream", headers=SSE_HEADERS
            )

        @router.get(
            "/{subject_id:path}", response_model=SubjectResponse, response_model_by_alias=True
        )
        async def read_subject(subject_id: str) -> SubjectResponse:
            response = await subject_response(subject_id)
            if not response:
                raise HTTPException(status.HTTP_404_NOT_FOUND, f"No {subject_type} {subject_id!r}.")
            return response

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
        subject: "Subject | StoredSubject | None" = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Record one of this extension's domain events to the log, stamped with the
        extension automatically. Apps record through here so the ``Event`` model
        stays a platform internal. ``type`` is the milestone's own word ("merged") —
        the feed reads it as one, so an extension writes no rendering."""
        Event.emit(
            type=type,
            subject=subject.identity if subject else None,
            label=subject.label if subject else None,
            payload=payload,
            extension=cls.name,
        )

    @classmethod
    async def get_subject_activity(
        cls, subject: "Subject | StoredSubject"
    ) -> "SubjectActivity | None":
        """The subject's live sub-phase, if any (e.g. "Building sandbox VM…"). Optional —
        override to surface a transient signal the running run pushes."""
        return
