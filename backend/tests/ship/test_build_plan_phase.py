from types import SimpleNamespace

import pytest
from druks.contrib.ship.contracts import (
    AcceptanceCriterionOutput,
    PlanData,
    QuestionOptionOutput,
    QuestionOutput,
    ReviewOutput,
    ReviewWork,
)
from druks.contrib.ship.enums import ReviewDecision
from druks.contrib.ship.extension import Ship
from druks.contrib.ship.policy import PlanGate, RepoPolicy
from druks.contrib.ship.workflows import Build
from druks.workflows import FatalError, OperatorReply, Run


def _flow(*, plan_gate: PlanGate = "human") -> Build:
    flow = Build()
    # plan_approval is undeclared, so the workflow setting resolves the gate.
    flow._policy = RepoPolicy()
    flow._settings = Build.Settings(plan_gate=plan_gate)
    return flow


def _fake_plans(monkeypatch, *plans: PlanData) -> list[dict]:
    passes: list[dict] = []
    supply = iter(plans)

    async def fake_plan_agent(**kwargs):
        passes.append(kwargs)
        return next(supply)

    monkeypatch.setattr(Ship, "generate_plan", fake_plan_agent)
    return passes


def _fake_grades(monkeypatch, *grades: ReviewOutput) -> None:
    supply = iter(grades)

    async def fake_review_agent():
        return next(supply)

    monkeypatch.setattr(Ship, "review_plan", fake_review_agent)


def _no_review_agent(monkeypatch) -> None:
    async def fail_review_agent():
        raise AssertionError("review_plan must not run here")

    monkeypatch.setattr(Ship, "review_plan", fail_review_agent)


def _acceptance_criteria() -> list[AcceptanceCriterionOutput]:
    return [
        AcceptanceCriterionOutput(
            id="AC-1",
            description="The planned change is implemented.",
            verification="Read the changed code.",
        )
    ]


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
        PlanData(plan_markdown="v3", acceptance_criteria=_acceptance_criteria()),
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
            acceptance_criteria=_acceptance_criteria(),
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


async def test_approve_confirming_recommendations_on_empty_plan_redrafts(monkeypatch):
    """Approving recommended options on an empty-AC plan triggers a second plan."""
    flow = _flow()
    passes = _fake_plans(
        monkeypatch,
        PlanData(
            plan_markdown="blocked",
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
        PlanData(
            plan_markdown="v2",
            acceptance_criteria=_acceptance_criteria(),
        ),
    )
    _no_review_agent(monkeypatch)
    replies = iter(
        [
            OperatorReply(action="approve", answers={"q1": "a"}),
            OperatorReply(action="approve"),
        ]
    )

    async def fake_review(*, questions=None, context=""):
        return next(replies)

    flow.review = fake_review

    assert await flow._plan_phase() is True
    assert len(passes) == 2
    assert passes[1]["answered_questions"] == [{"question": "Which cache?", "answer": "Redis"}]


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
        PlanData(plan_markdown="v2", acceptance_criteria=_acceptance_criteria()),
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
        PlanData(plan_markdown="v2", acceptance_criteria=_acceptance_criteria()),
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


async def test_machine_mode_folds_the_critique_into_one_redraft(monkeypatch):
    """Machine mode, no questions: REQUEST_CHANGES routes the critique into one
    redraft (reviewer_notes) that implements without re-review, and nothing
    parks — the reviewer's single grade is all the loop may consume."""
    flow = _flow(plan_gate="machine")
    passes = _fake_plans(monkeypatch, PlanData(plan_markdown="v1"), PlanData(plan_markdown="v2"))
    _fake_grades(
        monkeypatch,
        ReviewOutput(decision=ReviewDecision.REQUEST_CHANGES, body="name the wire schema"),
    )

    async def fail_park(*, questions=None, context=""):
        raise AssertionError("a machine-mode plan must not park")

    flow.review = fail_park

    assert await flow._plan_phase() is True
    assert [p["reviewer_notes"] for p in passes] == ["", "name the wire schema"]


async def test_machine_mode_redraft_questions_park_without_the_folded_critique(monkeypatch):
    """A redraft's questions park without repeating the critique already folded
    into it; the answered redraft implements without a second review."""
    flow = _flow(plan_gate="machine")
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
    )
    parks: list[list[QuestionOutput]] = []

    async def fake_review(*, questions=None, context=""):
        parks.append(list(questions or []))
        return OperatorReply(action="request_changes", answers={"q1": "behind a flag"})

    flow.review = fake_review

    assert await flow._plan_phase() is True
    assert len(parks) == 1
    assert [question.id for question in parks[0]] == ["q1"]
    assert [p["reviewer_notes"] for p in passes] == [
        "",
        "name the rollout boundary",
        "",
    ]


async def test_adaptive_high_confidence_approved_plan_implements_without_parking(monkeypatch):
    """Adaptive: the critic's approval plus the planner's own high confidence
    skip the operator entirely."""
    flow = _flow(plan_gate="adaptive")
    _fake_plans(
        monkeypatch,
        PlanData(
            plan_markdown="v1",
            acceptance_criteria=_acceptance_criteria(),
            confidence="high",
        ),
    )
    _fake_grades(monkeypatch, ReviewOutput(decision=ReviewDecision.APPROVE, body=""))

    async def fail_park(*, questions=None, context=""):
        raise AssertionError("a high-confidence approved plan must not park")

    flow.review = fail_park

    assert await flow._plan_phase() is True


async def test_adaptive_questions_park_before_confidence_counts(monkeypatch):
    """Adaptive: open questions always park — confidence never outranks them."""
    flow = _flow(plan_gate="adaptive")
    _fake_plans(
        monkeypatch,
        PlanData(
            plan_markdown="v1",
            confidence="high",
            questions=[QuestionOutput(id="q1", prompt="Feature flag?", options=[])],
        ),
        PlanData(
            plan_markdown="v2",
            acceptance_criteria=_acceptance_criteria(),
            confidence="high",
        ),
    )
    _fake_grades(monkeypatch, ReviewOutput(decision=ReviewDecision.APPROVE, body=""))
    parks: list[list[QuestionOutput]] = []
    replies = iter(
        [
            OperatorReply(action="request_changes", answers={"q1": "behind a flag"}),
        ]
    )

    async def fake_review(*, questions=None, context=""):
        parks.append(list(questions or []))
        return next(replies)

    flow.review = fake_review

    assert await flow._plan_phase() is True
    assert [question.id for question in parks[0]] == ["q1"]
    assert len(parks) == 1


async def test_adaptive_confident_plan_without_acceptance_criteria_parks(monkeypatch):
    """Adaptive: skipping the operator takes the same bar the human path holds —
    no acceptance criteria, no auto-proceed."""
    flow = _flow(plan_gate="adaptive")
    _fake_plans(
        monkeypatch,
        PlanData(plan_markdown="v1", confidence="high"),
        PlanData(
            plan_markdown="v2",
            acceptance_criteria=_acceptance_criteria(),
            confidence="high",
        ),
    )
    _fake_grades(monkeypatch, ReviewOutput(decision=ReviewDecision.APPROVE, body=""))
    parks = 0

    async def fake_review(*, questions=None, context=""):
        nonlocal parks
        parks += 1
        return OperatorReply(action="approve")

    flow.review = fake_review

    assert await flow._plan_phase() is True
    assert parks == 2


@pytest.mark.parametrize("confidence", ["medium", "low"])
async def test_adaptive_parks_unless_the_planner_is_confident(monkeypatch, confidence):
    """Adaptive: a critic-approved plan still parks when the planner hedges."""
    flow = _flow(plan_gate="adaptive")
    _fake_plans(
        monkeypatch,
        PlanData(
            plan_markdown="v1",
            acceptance_criteria=_acceptance_criteria(),
            confidence=confidence,
        ),
    )
    _fake_grades(monkeypatch, ReviewOutput(decision=ReviewDecision.APPROVE, body=""))
    parks: list[list[QuestionOutput]] = []

    async def fake_review(*, questions=None, context=""):
        parks.append(list(questions or []))
        return OperatorReply(action="approve")

    flow.review = fake_review

    assert await flow._plan_phase() is True
    assert parks == [[]]


async def test_adaptive_critic_concerns_park_even_at_high_confidence(monkeypatch):
    """Adaptive: a critique means the operator looks — the folded redraft parks
    no matter how confident it claims to be."""
    flow = _flow(plan_gate="adaptive")
    passes = _fake_plans(
        monkeypatch,
        PlanData(plan_markdown="v1", confidence="high"),
        PlanData(
            plan_markdown="v2",
            acceptance_criteria=_acceptance_criteria(),
            confidence="high",
        ),
    )
    _fake_grades(
        monkeypatch,
        ReviewOutput(decision=ReviewDecision.REQUEST_CHANGES, body="wrong layer"),
    )
    parks: list[list[QuestionOutput]] = []

    async def fake_review(*, questions=None, context=""):
        parks.append(list(questions or []))
        return OperatorReply(action="approve")

    flow.review = fake_review

    assert await flow._plan_phase() is True
    assert [p["reviewer_notes"] for p in passes] == ["", "wrong layer"]
    assert parks == [[]]


@pytest.mark.parametrize("plan_gate", ["machine", "machine_then_human", "adaptive"])
async def test_questions_park_before_machine_review(monkeypatch, plan_gate):
    """Open questions always park for the operator — the machine reviewer only
    ever sees a question-free plan."""
    flow = _flow(plan_gate=plan_gate)
    _fake_plans(
        monkeypatch,
        PlanData(
            plan_markdown="v1",
            questions=[QuestionOutput(id="q1", prompt="Feature flag?", options=[])],
        ),
        PlanData(plan_markdown="v2", acceptance_criteria=_acceptance_criteria()),
    )
    _fake_grades(monkeypatch, ReviewOutput(decision=ReviewDecision.APPROVE, body=""))
    replies = iter(
        [
            OperatorReply(action="request_changes", answers={"q1": "yes, behind a flag"}),
            OperatorReply(action="approve"),
        ]
    )
    parks: list[list[QuestionOutput]] = []

    async def fake_review(*, questions=None, context=""):
        parks.append(list(questions or []))
        return next(replies)

    flow.review = fake_review

    assert await flow._plan_phase() is True
    assert [question.id for question in parks[0]] == ["q1"]
    assert len(parks) == (1 if plan_gate == "machine" else 2)


async def test_machine_then_human_approval_parks_for_operator_approval(monkeypatch):
    """A machine-approved plan still parks for operator approval."""
    flow = _flow(plan_gate="machine_then_human")
    _fake_plans(
        monkeypatch,
        PlanData(plan_markdown="v1", acceptance_criteria=_acceptance_criteria()),
    )
    _fake_grades(monkeypatch, ReviewOutput(decision=ReviewDecision.APPROVE, body=""))
    parks: list[tuple[list, str]] = []

    async def fake_review(*, questions=None, context=""):
        parks.append((list(questions or []), context))
        return OperatorReply(action="approve")

    flow.review = fake_review

    assert await flow._plan_phase() is True
    assert parks == [([], "")]


async def test_machine_then_human_critiques_force_one_redraft_before_the_park(monkeypatch):
    """The machine critique is folded into one redraft that parks for the
    operator without a re-review."""
    flow = _flow(plan_gate="machine_then_human")
    passes = _fake_plans(
        monkeypatch,
        PlanData(plan_markdown="v1"),
        PlanData(plan_markdown="v2", acceptance_criteria=_acceptance_criteria()),
    )
    _fake_grades(
        monkeypatch,
        ReviewOutput(decision=ReviewDecision.REQUEST_CHANGES, body="name the wire schema"),
    )
    parks: list[list[QuestionOutput]] = []

    async def fake_review(*, questions=None, context=""):
        parks.append(list(questions or []))
        return OperatorReply(action="approve")

    flow.review = fake_review

    assert await flow._plan_phase() is True
    assert [passing["reviewer_notes"] for passing in passes] == ["", "name the wire schema"]
    assert parks == [[]]


async def test_operator_rounds_after_the_critique_never_rerun_the_reviewer(monkeypatch):
    """The reviewer's pass is spent: operator request_changes rounds redraft
    and re-park on the operator alone."""
    flow = _flow(plan_gate="machine_then_human")
    passes = _fake_plans(
        monkeypatch,
        PlanData(plan_markdown="v1"),
        PlanData(plan_markdown="v2", acceptance_criteria=_acceptance_criteria()),
        PlanData(plan_markdown="v3", acceptance_criteria=_acceptance_criteria()),
    )
    _fake_grades(
        monkeypatch,
        ReviewOutput(decision=ReviewDecision.REQUEST_CHANGES, body="wrong layer"),
    )
    replies = iter(
        [
            OperatorReply(action="request_changes", note="steer left"),
            OperatorReply(action="approve"),
        ]
    )
    parks = 0

    async def fake_review(*, questions=None, context=""):
        nonlocal parks
        parks += 1
        return next(replies)

    flow.review = fake_review

    assert await flow._plan_phase() is True
    assert parks == 2
    assert [passing["reviewer_notes"] for passing in passes] == ["", "wrong layer", ""]
    assert [passing["operator_note"] for passing in passes] == ["", "", "steer left"]


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
    flow = Build()

    async def bailed():
        return SimpleNamespace(
            status="needs_clarification",
            summary="AC-3 requires a pure function that performs I/O",
        )

    monkeypatch.setattr(Ship, "implement", bailed)
    with pytest.raises(FatalError, match="pure function"):
        await flow.implement()


async def test_a_rejected_merge_intent_reparks_the_work_gate(monkeypatch):
    """GitHub declining the merge sends the operator back to the work gate,
    rather than finishing a run whose PR never merged."""
    flow = Build()
    flow._policy = RepoPolicy()
    flow.subject = SimpleNamespace(repo="clawhaven/example")
    flow.journal = SimpleNamespace(implementations=[])
    reparked = []

    async def declined():
        return False

    async def fake_work_gate():
        reparked.append(True)
        return True

    monkeypatch.setattr(flow, "declare_merge_intent", declined)
    monkeypatch.setattr(flow, "_work_gate", fake_work_gate)

    assert await flow._approved_work() is True
    assert reparked == [True]
