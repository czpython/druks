import importlib

import pytest

# The documented v1 app-author surface: each concern namespace and the
# exact names it exports. Pattern A — no root facade; druks stays thin.
AUTHOR_SURFACE = {
    "druks.apps": {"App", "AppSettings", "Secret"},
    "druks.browser": {"BrowserSession", "BrowserSessionSignedOutError"},
    "druks.services": {
        "OauthClient",
        "OauthExchangeError",
        "OauthRefreshError",
        "Service",
        "ServiceConnectError",
        "ServiceNotConnectedError",
    },
    "druks.sandbox": {"Sandbox"},
    "druks.agents": {"Agent", "AgentOutput"},
    "druks.workflows": {
        "AgentCall",
        "AgentCallResponse",
        "AgentCallStatus",
        "FatalError",
        "Gate",
        "Journal",
        "OperatorReply",
        "RunResponse",
        "Subject",
        "SubjectActivity",
        "SubjectStatus",
        "SubjectSummary",
        "Workflow",
        "WorkflowError",
        "WorkflowEvent",
        "set_run_phase",
        "step",
        "task",
    },
    "druks.db": {"Base", "StoredSubject", "db_session"},
    "druks.schemas": {"BaseResponse"},
    "druks.ui": {"Page", "page"},
    "druks.signals": {"subscribe"},
    "druks.events": {"Event"},
    "druks.files": {"File", "FileField"},
    "druks.prompts": {"render_prompt"},
}


@pytest.mark.parametrize("module_name, exports", AUTHOR_SURFACE.items())
def test_namespace_matches_author_surface(module_name, exports):
    module = importlib.import_module(module_name)
    assert set(module.__all__) == exports
    for name in exports:
        assert getattr(module, name) is not None


def test_root_druks_stays_thin():
    # druks is a service, not a library — the root exports only its version; the
    # author API lives in the concern namespaces above.
    import druks

    assert druks.__all__ == ["__version__"]
