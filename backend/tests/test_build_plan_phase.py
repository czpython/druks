from types import SimpleNamespace

import pytest
from druks.build.contracts import PlanData, QuestionOptionOutput, QuestionOutput, ReviewOutput
from druks.build.enums import ReviewDecision
from druks.build.policy import RepoPolicy
from druks.build.workflows import Build, BuildWorkflow
from druks.workflows import FatalError, OperatorReply


def _flow(*, auto_dispatch: bool = False) -> BuildWorkflow:
    flow = BuildWorkflow()
    # plan_approval is undeclared: the gate resolves to "none" iff auto_dispatch.
    flow._policy = RepoPolicy()
    flow._settings = BuildWorkflow.Settings(auto_dispatch_on_plan_approval=auto_dispatch)
    return flow


def _fake_plans(monkeypatch, *plans: PlanData) -> list[dict]:
    passes: list[dict] = []
    supply = iter(plans)

    async def fake_plan_agent(**kwargs):
        passes.append(kwargs)
        return next(supply)

    monkeypatch.setattr(Build, "generate_plan", fake_plan_agent)
    return passes


def _fake_grades(monkeypatch, *grades: ReviewOutput) -> None:
    supply = iter(grades)

    async def fake_review_agent():
        return next(supply)

    monkeypatch.setattr(Build, "review_plan", fake_review_agent)


def _no_review_agent(monkeypatch) -> None:
    async def fail_review_agent():
        raise AssertionError("review_plan must not run here")

    monkeypatch.setattr(Build, "review_plan", fail_review_agent)


async def test_gate_mode_parks_every_plan_and_never_calls_the_reviewer(monkeypatch):
    """Gate mode: generate → park, the reviewer never runs; operator answers
    and notes thread into the next pass."""
    flow = _flow()
    passes = _fake_plans(
        monkeypatch,
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
    )
    _no_review_agent(monkeypatch)

    replies = iter(
        [
            OperatorReply(
                action="request_changes", answers={"q1": "memcache — redis is banned here"}
            ),
            OperatorReply(action="request_changes", note="add a rollback section"),
            OperatorReply(action="approve"),
        ]
    )

    async def fake_review(*, questions=None, context=""):
        return next(replies)

    flow.review = fake_review

    assert await flow._plan_phase() is True
    assert passes == [
        {"answered_questions": [], "operator_note": "", "reviewer_notes": ""},
        {
            "answered_questions": [
                {"question": "Which cache?", "answer": "memcache — redis is banned here"}
            ],
            "operator_note": "",
            "reviewer_notes": "",
        },
        {"answered_questions": [], "operator_note": "add a rollback section", "reviewer_notes": ""},
    ]


async def test_auto_mode_folds_the_critique_into_one_redraft(monkeypatch):
    """Auto mode, no questions: REQUEST_CHANGES routes the critique into one
    redraft (reviewer_notes), the re-review approves, and nothing parks."""
    flow = _flow(auto_dispatch=True)
    passes = _fake_plans(monkeypatch, PlanData(plan_markdown="v1"), PlanData(plan_markdown="v2"))
    _fake_grades(
        monkeypatch,
        ReviewOutput(decision=ReviewDecision.REQUEST_CHANGES, body="name the wire schema"),
        ReviewOutput(decision=ReviewDecision.APPROVE, body=""),
    )

    async def fail_park(*, questions=None, context=""):
        raise AssertionError("an approved auto-mode plan must not park")

    flow.review = fail_park

    assert await flow._plan_phase() is True
    assert [p["reviewer_notes"] for p in passes] == ["", "name the wire schema"]


async def test_auto_mode_parks_after_the_bounded_redraft(monkeypatch):
    """Two straight rejections exhaust the machine loop — the run parks with the
    critique standing. The operator's request_changes re-arms one fresh redraft."""
    flow = _flow(auto_dispatch=True)
    passes = _fake_plans(
        monkeypatch,
        PlanData(plan_markdown="v1"),
        PlanData(plan_markdown="v2"),
        PlanData(plan_markdown="v3"),
        PlanData(plan_markdown="v4"),
    )
    _fake_grades(
        monkeypatch,
        ReviewOutput(decision=ReviewDecision.REQUEST_CHANGES, body="critique-1"),
        ReviewOutput(decision=ReviewDecision.REQUEST_CHANGES, body="critique-2"),
        ReviewOutput(decision=ReviewDecision.REQUEST_CHANGES, body="critique-3"),
        ReviewOutput(decision=ReviewDecision.APPROVE, body=""),
    )
    parks: list[tuple[list, str]] = []
    replies = iter([OperatorReply(action="request_changes", note="steer left")])

    async def fake_review(*, questions=None, context=""):
        parks.append((list(questions or []), context))
        return next(replies)

    flow.review = fake_review

    assert await flow._plan_phase() is True
    # One park, after the exhausted redraft, carrying the final critique.
    assert parks == [([], "critique-2")]
    assert [p["reviewer_notes"] for p in passes] == ["", "critique-1", "", "critique-3"]
    assert [p["operator_note"] for p in passes] == ["", "", "steer left", "steer left"]


async def test_questions_park_in_auto_mode_too(monkeypatch):
    """Open questions always park for the operator — the machine reviewer only
    ever sees a question-free plan."""
    flow = _flow(auto_dispatch=True)
    _fake_plans(
        monkeypatch,
        PlanData(
            plan_markdown="v1",
            questions=[QuestionOutput(id="q1", prompt="Feature flag?", options=[])],
        ),
        PlanData(plan_markdown="v2"),
    )
    _fake_grades(monkeypatch, ReviewOutput(decision=ReviewDecision.APPROVE, body=""))
    replies = iter([OperatorReply(action="request_changes", answers={"q1": "yes, behind a flag"})])

    async def fake_review(*, questions=None, context=""):
        assert questions  # the park carries the open questions
        return next(replies)

    flow.review = fake_review

    assert await flow._plan_phase() is True


async def test_cancel_at_the_plan_park_stops_the_run(monkeypatch):
    flow = _flow()
    _fake_plans(monkeypatch, PlanData(plan_markdown="v1"))
    _no_review_agent(monkeypatch)

    async def fake_review(*, questions=None, context=""):
        return OperatorReply(action="cancel")

    flow.review = fake_review

    with pytest.raises(FatalError, match="cancelled at plan review"):
        await flow._plan_phase()


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
