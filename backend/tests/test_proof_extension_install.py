import subprocess
import sys
import textwrap
from importlib.metadata import entry_points

import pytest

# The one test that proves the *editable install itself* works. It runs only when
# druks-field_notes is actually pip-installed (the dedicated CI ``pip install -e``
# step); the plain suite skips it. The check runs in a fresh subprocess — a clean
# interpreter with only the install, no in-session registry state or monkeypatched
# ``entry_points`` from the rest of the proof suite — so discovery here can only come
# from the installed dist metadata, the registration an author gets from the install
# alone.


def _installed_entry():
    return next(
        (entry for entry in entry_points(group="druks.extensions") if entry.name == "field_notes"),
        None,
    )


# DB-free surfaces only: a clean process has no database bind, and the in-process tests
# already cover the settings-value and migration reads that need one.
_CHECK = textwrap.dedent(
    """
    from druks.extensions.loader import iter_extensions, load_extension

    names = {extension.name for extension in iter_extensions()}
    assert "field_notes" in names, names

    extension = load_extension("field_notes")
    assert extension.subject.subject_type == "note"
    assert extension.settings_model is not None
    assert list(extension.settings_model.model_fields) == ["board_size", "visibility", "sync_token"]
    assert [workflow.__name__ for workflow in extension.workflows()] == ["Summarize"]
    assert {router.prefix for router in extension.routers()} >= {"/notes", "/note"}
    assert extension.migrations_dir() is not None
    print("ok")
    """
)


@pytest.mark.skipif(
    _installed_entry() is None,
    reason="field_notes is not pip-installed; the CI editable-install step exercises this",
)
def test_editable_install_boots_and_discovers_in_a_clean_process():
    """A fresh interpreter with the package installed boots the loader, discovers
    field_notes off its dist metadata, and reads every surface — no test seam, the
    exact registration ``pip install -e`` gives an author."""
    result = subprocess.run(
        [sys.executable, "-c", _CHECK],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
