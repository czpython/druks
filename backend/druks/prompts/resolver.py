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
    # each mounted under the app's name: ``ship/build/implement.md`` is
    # ``build/implement.md`` inside ship's package, so nothing repeats the app in
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
    repo: str | None = None,
    **context: object,
) -> str:
    """Render a prompt template through the override hierarchy.

    Resolution order (first found wins), always against default branches:

    1. ``<repo>/.druks/<app>/prompts/<rest>``           — repo-specific tuning
    2. ``<owner>/.druks`` repo ``<app>/prompts/<rest>`` — org-wide tuning
    3. ``<rest>`` under the app's own ``<package>/templates`` root — built-in baseline

    A 404 at a tier silently falls through to the next. Auth or network
    failures propagate — those are real misconfigurations and the
    caller should decide whether to retry, fall back, or fail.
    """
    # Templates routinely reference ``{{ repo }}``; the kwarg drives
    # override resolution AND lands in the render context so callers
    # don't pass it twice.
    if repo:
        context.setdefault("repo", repo)
    override = await _resolve_override(name, repo=repo)
    if override:
        return await _environment().from_string(override).render_async(**context)
    return await _environment().get_template(name).render_async(**context)


async def _resolve_override(name: str, *, repo: str | None) -> str | None:
    namespaced = _app_prompt_path(name)
    if repo and namespaced:
        owner = repo.partition("/")[0]
        body = await fetch_file(repo=repo, path=f".druks/{namespaced}")
        return body or await fetch_file(repo=f"{owner}/.druks", path=namespaced)
    return


def _app_prompt_path(name: str) -> str | None:
    """Where a bundled template's repo override lives. Bundled prompts are
    namespaced by app (``<app>/<rest>``), and an app owns ``.druks/<app>/``,
    so the override is ``<app>/prompts/<rest>`` — derived from the name, no table
    to keep in sync. A name with no app segment (no ``/``) isn't overridable."""
    app, _, rest = name.partition("/")
    if rest:
        return f"{app}/prompts/{rest}"
    return
