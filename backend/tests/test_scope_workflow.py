from druks.build.workflows import Scope
from druks.extensions.registry import workflows


def test_scope_workflow_registered():
    # The kind derives from the class name + namespaces by extension to "build.scope".
    assert Scope.kind == "build.scope"
    assert workflows.get("build.scope") is Scope
