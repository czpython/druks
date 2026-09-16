from types import SimpleNamespace

import pytest
from druks.contrib.software_factory.app import SoftwareFactory
from druks.contrib.software_factory.enums import EvaluationVerdict
from druks.contrib.software_factory.workflows import Build


@pytest.mark.parametrize("verdict", list(EvaluationVerdict))
@pytest.mark.parametrize("limit", [1, 3])
async def test_evaluator_and_workflow_share_the_remaining_rework_budget(
    monkeypatch, verdict, limit
):
    workflow = Build()
    workflow._settings = Build.Settings(max_implementation_revisions=limit)
    workflow.journal = SimpleNamespace(implementation_revision=0)
    rounds = []
    handoffs = []

    async def implement():
        workflow.journal.implementation_revision += 1

    async def evaluate(*, can_rework):
        rounds.append(can_rework)
        return SimpleNamespace(verdict=verdict)

    async def review():
        handoffs.append(workflow.journal.implementation_revision)
        return True

    monkeypatch.setattr(workflow, "implement", implement)
    monkeypatch.setattr(SoftwareFactory, "evaluate_implementation", evaluate)
    monkeypatch.setattr(workflow, "_work_gate", review)

    await workflow._implement_phase()

    expected_rounds = limit if verdict == EvaluationVerdict.FAIL else 1
    assert rounds == [revision < limit for revision in range(1, expected_rounds + 1)]
    assert handoffs == [expected_rounds]
