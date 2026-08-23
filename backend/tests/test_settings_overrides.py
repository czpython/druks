from druks.user_settings.models import SettingsOverride

# The override→declared→harness resolution chains (model, effort, timeout) are
# pinned at the wire in test_api_settings.py; this file keeps only the model
# primitives with no API-level twin.


def test_app_setting_override_then_default(druks_db):
    # No override → the declared default passed by the caller.
    assert SettingsOverride.app_setting("ship", "auto_merge", True, is_secret=False) is True
    # An override wins — including turning it off.
    SettingsOverride.set_app_setting("ship", "auto_merge", False, is_secret=False)
    assert SettingsOverride.app_setting("ship", "auto_merge", True, is_secret=False) is False
    # Clearing reverts to the caller's default.
    SettingsOverride.set_app_setting("ship", "auto_merge", None, is_secret=False)
    assert SettingsOverride.app_setting("ship", "auto_merge", True, is_secret=False) is True


def test_workflow_setting_namespaced_by_kind(druks_db):
    SettingsOverride.set_workflow_setting("ship.build", "shared", "a")
    SettingsOverride.set_workflow_setting("other_workflow", "shared", "b")
    assert SettingsOverride.workflow_setting("ship.build", "shared", None) == "a"
    assert SettingsOverride.workflow_setting("other_workflow", "shared", None) == "b"
