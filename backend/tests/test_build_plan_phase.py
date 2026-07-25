from types import SimpleNamespace

import pytest
from druks.build.contracts import (
    PlanData,
    QuestionOptionOutput,
    QuestionOutput,
    ReviewOutput,
    ReviewWork,
)
from druks.build.enums import ReviewDecision
from druks.build.policy import RepoPolicy
from druks.build.workflows import Build, BuildWorkflow
from druks.workflows import FatalError, OperatorReply, Run


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
                    options=[QuestionOptionOutput(id="a", label="Redis", recommended=False)],
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


async def test_approve_confirming_recommendations_proceeds_without_redraft(monkeypatch):
    """Approving every recommended option proceeds after one plan."""
    flow = _flow()
    passes = _fake_plans(
        monkeypatch,
        PlanData(
            plan_markdown="v1",
            questions=[
                QuestionOutput(
                    id="q1",
                    prompt="Which cache?",
                    options=[
                        QuestionOptionOutput(id="a", label="Redis", recommended=True),
                        QuestionOptionOutput(id="b", label="Memcache", recommended=False),
                    ],
                )
            ],
        ),
    )
    _no_review_agent(monkeypatch)

    async def fake_review(*, questions=None, context=""):
        return OperatorReply(action="approve", answers={"q1": "a"})

    flow.review = fake_review

    assert await flow._plan_phase() is True
    assert len(passes) == 1


async def test_approve_diverging_from_recommendation_redrafts(monkeypatch):
    """Approving a different option folds that answer into a second plan."""
    flow = _flow()
    passes = _fake_plans(
        monkeypatch,
        PlanData(
            plan_markdown="v1",
            questions=[
                QuestionOutput(
                    id="q1",
                    prompt="Which cache?",
                    options=[
                        QuestionOptionOutput(id="a", label="Redis", recommended=True),
                        QuestionOptionOutput(id="b", label="Memcache", recommended=False),
                    ],
                )
            ],
        ),
        PlanData(plan_markdown="v2"),
    )
    _no_review_agent(monkeypatch)
    replies = iter(
        [
            OperatorReply(action="approve", answers={"q1": "b"}),
            OperatorReply(action="approve"),
        ]
    )

    async def fake_review(*, questions=None, context=""):
        return next(replies)

    flow.review = fake_review

    assert await flow._plan_phase() is True
    assert len(passes) == 2
    assert passes[1]["answered_questions"] == [{"question": "Which cache?", "answer": "Memcache"}]


async def test_approve_with_note_redrafts(monkeypatch):
    """An approve note is change guidance and triggers a second plan."""
    flow = _flow()
    passes = _fake_plans(
        monkeypatch,
        PlanData(
            plan_markdown="v1",
            questions=[
                QuestionOutput(
                    id="q1",
                    prompt="Which cache?",
                    options=[QuestionOptionOutput(id="a", label="Redis", recommended=True)],
                )
            ],
        ),
        PlanData(plan_markdown="v2"),
    )
    _no_review_agent(monkeypatch)
    replies = iter(
        [
            OperatorReply(
                action="approve",
                answers={"q1": "a"},
                note="include the rollback command",
            ),
            OperatorReply(action="approve"),
        ]
    )

    async def fake_review(*, questions=None, context=""):
        return next(replies)

    flow.review = fake_review

    assert await flow._plan_phase() is True
    assert len(passes) == 2
    assert passes[1]["operator_note"] == "include the rollback command"


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


async def test_auto_mode_redraft_questions_park_without_the_folded_critique(monkeypatch):
    """A redraft's questions park without repeating the critique already folded into it."""
    flow = _flow(auto_dispatch=True)
    passes = _fake_plans(
        monkeypatch,
        PlanData(plan_markdown="v1"),
        PlanData(
            plan_markdown="v2",
            questions=[QuestionOutput(id="q1", prompt="Feature flag?", options=[])],
        ),
        PlanData(plan_markdown="v3"),
    )
    _fake_grades(
        monkeypatch,
        ReviewOutput(decision=ReviewDecision.REQUEST_CHANGES, body="name the rollout boundary"),
        ReviewOutput(decision=ReviewDecision.APPROVE, body=""),
    )
    parks: list[tuple[list, str]] = []

    async def fake_review(*, questions=None, context=""):
        parks.append((list(questions or []), context))
        return OperatorReply(action="request_changes", answers={"q1": "behind a flag"})

    flow.review = fake_review

    assert await flow._plan_phase() is True
    assert len(parks) == 1
    assert [question.id for question in parks[0][0]] == ["q1"]
    assert parks[0][1] == ""
    assert [p["reviewer_notes"] for p in passes] == [
        "",
        "name the rollout boundary",
        "",
    ]


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


async def test_review_work_ask_renders_a_notification_body(monkeypatch):
    workflow = _flow()
    input_requests: list[dict] = []

    async def fake_wait(*, input_request):
        input_requests.append(input_request)
        return ReviewWork(action="approve")

    async def fake_approved_work():
        return True

    monkeypatch.setattr(ReviewWork, "wait", fake_wait)
    monkeypatch.setattr(workflow, "_approved_work", fake_approved_work)

    await workflow._work_gate()

    run = Run(input_request=input_requests[0])
    assert run.get_rendered_ask()["body"]


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
