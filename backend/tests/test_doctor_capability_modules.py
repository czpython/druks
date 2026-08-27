import sys
from pathlib import Path
from types import SimpleNamespace

from druks import doctor
from druks.testing import make_settings

# A module that DEFINES a Webhook self-registers it as an import side effect, but
# autodiscover only imports leaf modules named for their role. Under the natural
# singular ``webhook.py`` the capability silently never registers — the check
# catches that by importing the off-canon leaf and introspecting what it defines.
_CAPABILITY_SOURCE = """\
from druks.webhooks.base import Webhook


class ThingHook(Webhook):
    provider = "thing"
    category = "events"

    def request_is_authentic(self) -> bool:
        return True

    def get_action(self) -> str:
        return "ping"
"""


def _temp_capability_package(
    tmp_path: Path, monkeypatch, *, module_name: str, source: str = _CAPABILITY_SOURCE
) -> str:
    """A real, importable one-module package whose capability lives in
    ``{module_name}.py`` — canonical (``webhooks``) or off-canon (``webhook``).
    Points the check's package walk at it and returns the package name."""
    package = "doctorprobe"
    pkg_dir = tmp_path / package
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("")
    (pkg_dir / f"{module_name}.py").write_text(source)

    monkeypatch.syspath_prepend(str(tmp_path))
    for name in list(sys.modules):
        if name == package or name.startswith(f"{package}."):
            del sys.modules[name]
    # The check reads packages off iter_apps at call time; the walk only
    # needs an object with a ``package``.
    monkeypatch.setattr(
        "druks.doctor.iter_apps",
        lambda: [SimpleNamespace(package=package)],
    )
    return package


def test_capability_under_off_canon_filename_is_flagged(tmp_path: Path, monkeypatch) -> None:
    _temp_capability_package(tmp_path, monkeypatch, module_name="webhook")
    settings = make_settings(tmp_path)

    result = doctor.check_capability_modules(settings)

    assert not result.ok
    assert "doctorprobe.webhook" in result.detail
    assert "rename to webhooks.py" in result.detail


def test_task_under_off_canon_filename_is_flagged(tmp_path: Path, monkeypatch) -> None:
    from druks.apps.loader import register_workflow_package

    _temp_capability_package(
        tmp_path,
        monkeypatch,
        module_name="task",
        source="from druks.workflows import task\n\n\n@task\nasync def tock() -> None: ...\n",
    )
    register_workflow_package("doctorprobe", "")
    settings = make_settings(tmp_path)

    result = doctor.check_capability_modules(settings)

    assert not result.ok
    assert "doctorprobe.task" in result.detail
    assert "rename to tasks.py" in result.detail


def test_capability_under_canonical_filename_passes(tmp_path: Path, monkeypatch) -> None:
    _temp_capability_package(tmp_path, monkeypatch, module_name="webhooks")
    settings = make_settings(tmp_path)

    result = doctor.check_capability_modules(settings)

    assert result.ok
    assert result.detail == "all capability files discoverable"


def test_capability_re_exported_by_a_role_module_is_not_flagged(
    tmp_path: Path, monkeypatch
) -> None:
    """The hook lives in an off-canon ``providers.py`` but a canonical
    ``webhooks.py`` imports it — so discovery reaches it transitively (the same
    way ``core/webhooks/__init__.py`` re-exports its handler modules). Not a stray."""
    package = "doctorprobe"
    pkg_dir = tmp_path / package
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("")
    (pkg_dir / "providers.py").write_text(_CAPABILITY_SOURCE)
    (pkg_dir / "webhooks.py").write_text(
        "from doctorprobe.providers import ThingHook  # noqa: F401"
    )

    monkeypatch.syspath_prepend(str(tmp_path))
    for name in list(sys.modules):
        if name == package or name.startswith(f"{package}."):
            del sys.modules[name]
    monkeypatch.setattr(
        "druks.doctor.iter_apps",
        lambda: [SimpleNamespace(package=package)],
    )
    settings = make_settings(tmp_path)

    result = doctor.check_capability_modules(settings)

    assert result.ok
