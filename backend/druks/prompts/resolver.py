import functools
import importlib.util
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, PrefixLoader, StrictUndefined
from jinja2.sandbox import ImmutableSandboxedEnvironment

from druks.apps.fetcher import fetch_file
from druks.apps.loader import iter_apps


@functools.cache
def _environment() -> Environment:
    # One Jinja environment over every installed app's own ``templates`` root,
    # each mounted under the app's name: ``software_factory/build/implement.md`` is
    # ``build/implement.md`` inside software_factory's package, so nothing repeats the app in
    # its own tree. Overrides resolved as strings via ``from_string`` still see the
    # loader for ``{% include %}`` against partials.
    #
    # Sandboxed because a ``.druks/<ext>/prompts/*`` override is authored by anyone with
    # push access to a monitored repo: the sandbox blocks the ``__globals__`` walk to
    # ``os.system``, and being immutable it blocks mutating the live ``workflow``/
    # ``workspace`` objects in context. Bundled templates only read public attributes,
    # so the sandbox is invisible to them.
    # ``enable_async``: attribute reads that return coroutines (the async
    # ``workflow.subject``) are awaited during render.
    return ImmutableSandboxedEnvironment(
        loader=PrefixLoader(_app_template_roots()),
        autoescape=False,
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
        enable_async=True,
    )


def _app_template_roots() -> dict[str, FileSystemLoader]:
    roots: dict[str, FileSystemLoader] = {}
    for app in iter_apps():
        spec = importlib.util.find_spec(app.package)
        if not spec or not spec.submodule_search_locations:
            continue
        root = Path(spec.submodule_search_locations[0]) / "templates"
        if root.is_dir():
            roots[app.name] = FileSystemLoader(root)
    return roots


async def render_prompt(
    name: str,
    /,
    *,
    overrides_from: str | None = None,
    **context: object,
) -> str:
    """Render a bundled prompt, with optional overrides from a named repository.

    Resolution order (first found wins), always against default branches:

    1. ``<repo>/.druks/<app>/prompts/<rest>``           — repo-specific tuning
    2. ``<owner>/.druks`` repo ``<app>/prompts/<rest>`` — org-wide tuning
    3. ``<rest>`` under the app's own ``<package>/templates`` root — built-in baseline

    A 404 at a tier silently falls through to the next. Auth or network
    failures propagate — those are real misconfigurations and the
    caller should decide whether to retry, fall back, or fail.
    """
    if overrides_from:
        app, _, rest = name.partition("/")
        if rest:
            owner = overrides_from.partition("/")[0]
            path = f"{app}/prompts/{rest}"
            override = await fetch_file(repo=overrides_from, path=f".druks/{path}")
            override = override or await fetch_file(repo=f"{owner}/.druks", path=path)
            if override:
                return await _environment().from_string(override).render_async(**context)
    return await _environment().get_template(name).render_async(**context)
