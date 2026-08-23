import subprocess
import sys
import textwrap

# DB-free surfaces only: a clean process has no database bind, and the in-process tests
# already cover the settings-value and migration reads that need one.
_CHECK = textwrap.dedent(
    """
    from druks.apps.loader import iter_apps, load_app

    names = {app.name for app in iter_apps()}
    assert "field_notes" in names, names

    app = load_app("field_notes")
    assert [subject.__name__ for subject in app.subjects()] == ["Note"]
    assert app.settings_model is not None
    assert list(app.settings_model.model_fields) == [
        "board_size",
        "visibility",
        "sync_signing_key",
        "sync_token",
    ]
    assert [workflow.__name__ for workflow in app.workflows()] == ["Summarize"]
    assert {router.prefix for router in app.routers()} >= {"/notes", "/note"}
    assert app.migrations_dir() is not None
    print("ok")
    """
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
