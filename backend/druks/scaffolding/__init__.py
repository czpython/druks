from importlib import metadata
from pathlib import Path

from druks.apps.base import NAME_RE

_TEMPLATE = Path(__file__).parent / "app_template"
# Django's ``startapp`` trick: template files carry a suffix so nothing in the
# template tree is importable or lintable as real code until it's rendered.
_TPL_SUFFIX = "-tpl"
# The template's package directory; renamed to ``druks_<name>`` on copy.
_PACKAGE_DIR = "package"


def create_app(name: str, parent: Path) -> Path:
    """Copy the app template to ``parent/druks-<name>`` and render its
    placeholders — a standalone package whose entry point self-registers with the
    platform on install. Raises ``ValueError`` on a bad name, a collision with an
    installed app, or an existing target directory."""
    if not NAME_RE.match(name):
        raise ValueError(
            f"app name {name!r} must match {NAME_RE.pattern!r} — it keys the "
            "/api/<name> namespace, the version table, and settings keys"
        )
    # Names only, no entry.load(): colliding with an installed app would break
    # boot, and listing entry points doesn't import anything.
    installed = {entry.name for entry in metadata.entry_points(group="druks.apps")}
    if name in installed:
        raise ValueError(f"app {name!r} is already installed")
    target = parent / f"druks-{name}"
    if target.exists():
        raise ValueError(f"{target} already exists")

    values = {
        "{{ name }}": name,
        "{{ Name }}": "".join(part.capitalize() for part in name.split("_")),
    }
    for source in sorted(_TEMPLATE.rglob("*")):
        if source.is_dir():
            continue
        parts = [
            f"druks_{name}" if part == _PACKAGE_DIR else part
            for part in source.relative_to(_TEMPLATE).parts
        ]
        if parts[-1].endswith(_TPL_SUFFIX):
            parts[-1] = parts[-1].removesuffix(_TPL_SUFFIX)
        content = source.read_text()
        for token, value in values.items():
            content = content.replace(token, value)
        destination = target.joinpath(*parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content)
    return target
