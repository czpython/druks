from types import SimpleNamespace

import pytest
from druks.build.contracts import PlanData, QuestionOptionOutput, QuestionOutput, ReviewOutput
from druks.build.enums import ReviewDecision
from druks.build.policy import RepoPolicy
from druks.build.workflows import Build, BuildWorkflow
from druks.workflows import FatalError


async def test_plan_phase_threads_free_text_into_the_next_pass(monkeypatch):
    """A free-text answer and a request_changes note both reach the next plan pass
    (answered_questions / operator_note) — a re-plan is never blind."""
    flow = BuildWorkflow()
    flow._policy = RepoPolicy()  # plan_approval defaults to the human gate
    flow._settings = BuildWorkflow.Settings()

    plans = iter(
        [
            PlanData(
                plan_markdown="v1",
                questions=[
                    QuestionOutput(
                        id="q1",
                        prompt="Which cache?",
                        options=[QuestionOptionOutput(id="a", label="Redis")],
                    )
                ],
            ),
            PlanData(plan_markdown="v2"),
            PlanData(plan_markdown="v3"),
        ]
    )
    passes: list[dict] = []

    async def fake_plan_agent():
        # The state the planner's view is built from on each pass.
        passes.append({"answered": flow._answered, "note": flow._note})
        return next(plans)

    async def fake_review_agent():
        return ReviewOutput(
            decision=ReviewDecision.REQUEST_CHANGES, body="", assignee_github_login=None
        )

    monkeypatch.setattr(Build, "generate_plan", fake_plan_agent)
    monkeypatch.setattr(Build, "review_plan", fake_review_agent)

    replies = iter(
        [
            {
                "action": "request_changes",
                "answers": {"q1": "memcache — redis is banned here"},
                "note": "",
            },
            {"action": "request_changes", "answers": {}, "note": "add a rollback section"},
            {"action": "approve", "answers": {}, "note": ""},
        ]
    )

    async def fake_review(*, questions=None):
        return next(replies)

    flow.review = fake_review

    assert await flow._plan_phase() is True
    assert passes == [
        {"answered": [], "note": ""},
        {
            "answered": [{"question": "Which cache?", "answer": "memcache — redis is banned here"}],
            "note": "",
        },
        {"answered": [], "note": "add a rollback section"},
    ]


async def test_needs_clarification_delivery_stops_the_run(monkeypatch):
    """The implementer bailing (needs_clarification) fails the run with its own
    reason — the stop is a workflow decision now, not a contract side effect."""
    flow = BuildWorkflow()

    async def bailed():
        return SimpleNamespace(
            status="needs_clarification",
            summary="AC-3 requires a pure function that performs I/O",
        )

    monkeypatch.setattr(Build, "implement", bailed)
    with pytest.raises(FatalError, match="pure function"):
        await flow.implement()
