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
    redraft (reviewer_notes), the re-review approves, and nothing parks."""
    flow = _flow(plan_gate="machine")
    passes = _fake_plans(monkeypatch, PlanData(plan_markdown="v1"), PlanData(plan_markdown="v2"))
    _fake_grades(
        monkeypatch,
        ReviewOutput(decision=ReviewDecision.REQUEST_CHANGES, body="name the wire schema"),
        ReviewOutput(decision=ReviewDecision.APPROVE, body=""),
    )

    async def fail_park(*, questions=None, context=""):
        raise AssertionError("an approved machine-mode plan must not park")

    flow.review = fail_park

    assert await flow._plan_phase() is True
    assert [p["reviewer_notes"] for p in passes] == ["", "name the wire schema"]


async def test_machine_mode_redraft_questions_park_without_the_folded_critique(monkeypatch):
    """A redraft's questions park without repeating the critique already folded into it."""
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


@pytest.mark.parametrize("plan_gate", ["machine", "machine_then_human"])
@pytest.mark.parametrize(
    ("operator_reply", "operator_note"),
    [
        pytest.param(OperatorReply(action="request_changes"), "", id="empty-request-changes"),
        pytest.param(
            OperatorReply(action="request_changes", note="steer left"),
            "steer left",
            id="guided-request-changes",
        ),
        pytest.param(
            OperatorReply(action="approve", note="steer left"),
            "steer left",
            id="approve-with-guidance",
        ),
    ],
)
async def test_machine_gate_preserves_bounded_critique_across_operator_park(
    monkeypatch, plan_gate, operator_reply, operator_note
):
    """The parked critique guides the first next-round draft before machine review."""
    flow = _flow(plan_gate=plan_gate)
    passes = _fake_plans(
        monkeypatch,
        PlanData(plan_markdown="v1"),
        PlanData(plan_markdown="v2"),
        PlanData(plan_markdown="v3"),
        PlanData(plan_markdown="v4", acceptance_criteria=_acceptance_criteria()),
    )
    _fake_grades(
        monkeypatch,
        ReviewOutput(decision=ReviewDecision.REQUEST_CHANGES, body="critique-1"),
        ReviewOutput(decision=ReviewDecision.REQUEST_CHANGES, body="critique-2"),
        ReviewOutput(decision=ReviewDecision.REQUEST_CHANGES, body="critique-3"),
        ReviewOutput(decision=ReviewDecision.APPROVE, body=""),
    )
    parks: list[tuple[list, str]] = []
    replies = iter(
        [operator_reply]
        + ([OperatorReply(action="approve")] if plan_gate == "machine_then_human" else [])
    )

    async def fake_review(*, questions=None, context=""):
        parks.append((list(questions or []), context))
        return next(replies)

    flow.review = fake_review

    assert await flow._plan_phase() is True
    expected_parks = [([], "critique-2")]
    if plan_gate == "machine_then_human":
        expected_parks.append(([], ""))
    assert parks == expected_parks
    assert [passing["reviewer_notes"] for passing in passes] == [
        "",
        "critique-1",
        "critique-2",
        "critique-3",
    ]
    assert [passing["operator_note"] for passing in passes] == [
        "",
        "",
        operator_note,
        operator_note,
    ]


async def test_bounded_critique_is_consumed_when_the_post_park_redraft_asks_a_question(
    monkeypatch,
):
    """A question emitted after the critique-backed park does not repeat that critique."""
    flow = _flow(plan_gate="machine")
    passes = _fake_plans(
        monkeypatch,
        PlanData(plan_markdown="v1"),
        PlanData(plan_markdown="v2"),
        PlanData(
            plan_markdown="v3",
            questions=[QuestionOutput(id="q1", prompt="Feature flag?", options=[])],
        ),
        PlanData(plan_markdown="v4", acceptance_criteria=_acceptance_criteria()),
    )
    _fake_grades(
        monkeypatch,
        ReviewOutput(decision=ReviewDecision.REQUEST_CHANGES, body="critique-1"),
        ReviewOutput(decision=ReviewDecision.REQUEST_CHANGES, body="critique-2"),
        ReviewOutput(decision=ReviewDecision.APPROVE, body=""),
    )
    replies = iter(
        [
            OperatorReply(action="request_changes"),
            OperatorReply(action="request_changes", answers={"q1": "behind a flag"}),
        ]
    )
    parks: list[tuple[list[QuestionOutput], str]] = []

    async def fake_review(*, questions=None, context=""):
        parks.append((list(questions or []), context))
        return next(replies)

    flow.review = fake_review

    assert await flow._plan_phase() is True
    assert [passing["reviewer_notes"] for passing in passes] == [
        "",
        "critique-1",
        "critique-2",
        "",
    ]
    assert parks[0] == ([], "critique-2")
    assert [question.id for question in parks[1][0]] == ["q1"]
    assert parks[1][1] == ""


@pytest.mark.parametrize("plan_gate", ["machine", "machine_then_human"])
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


async def test_machine_then_human_critiques_force_redrafts_before_the_park(monkeypatch):
    """Machine critique is folded into a redraft before operator approval."""
    flow = _flow(plan_gate="machine_then_human")
    passes = _fake_plans(
        monkeypatch,
        PlanData(plan_markdown="v1"),
        PlanData(plan_markdown="v2", acceptance_criteria=_acceptance_criteria()),
    )
    _fake_grades(
        monkeypatch,
        ReviewOutput(decision=ReviewDecision.REQUEST_CHANGES, body="name the wire schema"),
        ReviewOutput(decision=ReviewDecision.APPROVE, body=""),
    )
    parks: list[tuple[list, str]] = []

    async def fake_review(*, questions=None, context=""):
        parks.append((list(questions or []), context))
        return OperatorReply(action="approve")

    flow.review = fake_review

    assert await flow._plan_phase() is True
    assert [passing["reviewer_notes"] for passing in passes] == ["", "name the wire schema"]
    assert parks == [([], "")]


async def test_machine_then_human_exhaustion_parks_with_the_standing_critique(monkeypatch):
    """The final machine critique remains attached when the draft bound parks."""
    flow = _flow(plan_gate="machine_then_human")
    _fake_plans(
        monkeypatch,
        PlanData(plan_markdown="v1"),
        PlanData(plan_markdown="v2", acceptance_criteria=_acceptance_criteria()),
    )
    _fake_grades(
        monkeypatch,
        ReviewOutput(decision=ReviewDecision.REQUEST_CHANGES, body="critique-1"),
        ReviewOutput(decision=ReviewDecision.REQUEST_CHANGES, body="critique-2"),
    )
    parks: list[tuple[list, str]] = []

    async def fake_review(*, questions=None, context=""):
        parks.append((list(questions or []), context))
        return OperatorReply(action="approve")

    flow.review = fake_review

    assert await flow._plan_phase() is True
    assert parks == [([], "critique-2")]


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
