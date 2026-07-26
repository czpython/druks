import functools
import importlib.util
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, PrefixLoader, StrictUndefined
from jinja2.sandbox import ImmutableSandboxedEnvironment

from druks.extensions.fetcher import fetch_file
from druks.extensions.loader import iter_extensions


@functools.cache
def _environment() -> Environment:
    # One Jinja environment over every installed extension's own ``templates`` root,
    # each mounted under the extension's name: ``ship/build/implement.md`` is
    # ``build/implement.md`` inside ship's package, so nothing repeats the extension in
    # its own tree. Overrides resolved as strings via ``from_string`` still see the
    # loader for ``{% include %}`` against partials.
    #
    # Sandboxed because a ``.druks/<ext>/prompts/*`` override is authored by anyone with
    # push access to a monitored repo: the sandbox blocks the ``__globals__`` walk to
    # ``os.system``, and being immutable it blocks mutating the live ``workflow``/
    # ``workspace`` objects in context. Bundled templates only read public attributes,
    # so the sandbox is invisible to them.
    return ImmutableSandboxedEnvironment(
        loader=PrefixLoader(_extension_template_roots()),
        autoescape=False,
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _extension_template_roots() -> dict[str, FileSystemLoader]:
    roots: dict[str, FileSystemLoader] = {}
    for extension in iter_extensions():
        spec = importlib.util.find_spec(extension.package)
        if not spec or not spec.submodule_search_locations:
            continue
        root = Path(spec.submodule_search_locations[0]) / "templates"
        if root.is_dir():
            roots[extension.name] = FileSystemLoader(root)
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

    1. ``<repo>/.druks/<extension>/prompts/<rest>``           — repo-specific tuning
    2. ``<owner>/.druks`` repo ``<extension>/prompts/<rest>`` — org-wide tuning
    3. ``<rest>`` under the extension's own ``<package>/templates`` root — built-in baseline

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
        return _environment().from_string(override).render(**context)
    return _environment().get_template(name).render(**context)


async def _resolve_override(name: str, *, repo: str | None) -> str | None:
    namespaced = _extension_prompt_path(name)
    if not repo or not namespaced:
        return None
    owner = repo.partition("/")[0]
    body = await fetch_file(repo=repo, path=f".druks/{namespaced}")
    if body:
        return body
    return await fetch_file(repo=f"{owner}/.druks", path=namespaced)


def _extension_prompt_path(name: str) -> str | None:
    """Where a bundled template's repo override lives. Bundled prompts are
    namespaced by extension (``<extension>/<rest>``), and an extension owns ``.druks/<extension>/``,
    so the override is ``<extension>/prompts/<rest>`` — derived from the name, no table
    to keep in sync. A name with no extension segment (no ``/``) isn't overridable."""
    extension, _, rest = name.partition("/")
    if not rest:
        return None
    return f"{extension}/prompts/{rest}"
