from druks.user_settings.models import SettingsOverride

# The override→declared→harness resolution chains (model, effort, timeout) are
# pinned at the wire in test_api_settings.py; this file keeps only the model
# primitives with no API-level twin.


def test_extension_setting_override_then_default(db_session):
    # No override → the declared default passed by the caller.
    assert SettingsOverride.extension_setting("build", "auto_merge", True) is True
    # An override wins — including turning it off.
    SettingsOverride.set_extension_setting("build", "auto_merge", False)
    assert SettingsOverride.extension_setting("build", "auto_merge", True) is False
    # Clearing reverts to the caller's default.
    SettingsOverride.set_extension_setting("build", "auto_merge", None)
    assert SettingsOverride.extension_setting("build", "auto_merge", True) is True


def test_workflow_setting_namespaced_by_kind(db_session):
    SettingsOverride.set_workflow_setting("build_workflow", "shared", "a")
    SettingsOverride.set_workflow_setting("other_workflow", "shared", "b")
    assert SettingsOverride.workflow_setting("build_workflow", "shared", None) == "a"
    assert SettingsOverride.workflow_setting("other_workflow", "shared", None) == "b"
