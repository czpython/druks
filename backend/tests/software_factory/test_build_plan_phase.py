from types import SimpleNamespace

import pytest
from druks.contrib.software_factory.app import SoftwareFactory
from druks.contrib.software_factory.contracts import (
    AcceptanceCriterionOutput,
    CiReport,
    PlanData,
    PlanOutput,
    QuestionOptionOutput,
    QuestionOutput,
    ReviewOutput,
    ReviewReport,
    ReviewWork,
    TicketOutput,
)
from druks.contrib.software_factory.enums import ReviewDecision
from druks.contrib.software_factory.journal import BuildJournal
from druks.contrib.software_factory.policy import PlanGate, RepoPolicy
from druks.contrib.software_factory.workflows import Build, PullRequestReview
from druks.workflows import FatalError, OperatorReply, Run

from software_factory.factories import make_test_work_item


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

    monkeypatch.setattr(SoftwareFactory, "generate_plan", fake_plan_agent)
    return passes


def _fake_grades(monkeypatch, *grades: ReviewOutput) -> None:
    supply = iter(grades)

    async def fake_review_agent():
        return next(supply)

    monkeypatch.setattr(SoftwareFactory, "review_plan", fake_review_agent)


def _no_review_agent(monkeypatch) -> None:
    async def fail_review_agent():
        raise AssertionError("review_plan must not run here")

    monkeypatch.setattr(SoftwareFactory, "review_plan", fail_review_agent)


def _acceptance_criteria() -> list[AcceptanceCriterionOutput]:
    return [
        AcceptanceCriterionOutput(
            id="AC-1",
            description="The planned change is implemented.",
            verification="Read the changed code.",
        )
    ]


def _blocked_plan(reason: str) -> PlanData:
    # The agent call returns to_result(), so the status must survive the conversion.
    return PlanOutput(
        plan_markdown=reason,
        acceptance_criteria=[],
        questions=[],
        rejected_approaches=[],
        confidence="low",
        status="blocked",
        assignee_github_login=None,
        tickets=[],
    ).to_result()


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


def _split_plan() -> PlanData:
    return PlanData(
        plan_markdown="Split into the API and the web client.",
        tickets=[
            TicketOutput(repo="acme/api", title="Add the endpoint", description="d", blocked_by=[]),
            TicketOutput(
                repo="acme/web", title="Call the endpoint", description="d", blocked_by=["acme/api"]
            ),
        ],
    )


async def test_a_split_parks_for_a_person_under_the_machine_gate(monkeypatch):
    flow = _flow(plan_gate="machine")
    _fake_plans(monkeypatch, _split_plan())
    _no_review_agent(monkeypatch)
    asked = []

    async def fake_review(*, questions=None, context=""):
        asked.append(questions)
        return OperatorReply(action="approve")

    flow.review = fake_review

    assert await flow._plan_phase() is True
    assert asked == [[]]


async def test_an_approved_split_creates_tickets_instead_of_implementing():
    flow = Build()
    flow.journal = BuildJournal()
    flow.journal.add(_split_plan())
    splits = []

    async def load_policy_and_profile():
        return {"policy": RepoPolicy().model_dump(mode="json"), "profile": {}}

    async def load_settings():
        return Build.Settings()

    async def load_blockers():
        return []

    async def plan_phase():
        return True

    async def split():
        splits.append(True)

    async def implement_phase():
        raise AssertionError("a split opens no pull request")

    flow._load_policy_and_profile = load_policy_and_profile
    flow._load_settings = load_settings
    flow._load_blockers = load_blockers
    flow._plan_phase = plan_phase
    flow._split = split
    flow._implement_phase = implement_phase

    await flow.run_multistep()

    assert splits == [True]


async def test_a_split_creates_the_tickets_then_their_links_then_releases_the_spike():
    flow = Build()
    flow.journal = BuildJournal()
    flow.journal.add(_split_plan())
    keys = {"acme/api": "ENG-2", "acme/web": "ENG-3"}
    calls = []

    async def create_ticket(repo, title, description):
        calls.append(("create", repo))
        return keys[repo]

    async def add_blocker(key, blocker):
        calls.append(("link", key, blocker))

    async def release_ticket():
        calls.append(("release",))

    flow._create_ticket = create_ticket
    flow._add_blocker = add_blocker
    flow._release_ticket = release_ticket

    await flow._split()

    assert calls == [
        ("create", "acme/api"),
        ("create", "acme/web"),
        ("link", "ENG-3", "ENG-2"),
        ("release",),
    ]


async def test_each_split_ticket_takes_its_key_from_the_spike_and_its_repo(druks_db, monkeypatch):
    created = []

    class _Tracker:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return

        async def get_ticket(self, key):
            return {"team": "team-1"}

        async def create_ticket(self, key, **kwargs):
            created.append({"key": key, **kwargs})
            return "ENG-2"

    async def get_tracker(cls, source=None):
        return _Tracker()

    monkeypatch.setattr(SoftwareFactory, "get_tracker", classmethod(get_tracker))
    flow = Build()
    spike = await make_test_work_item(repo="acme/api", title="t", ticket_key="ENG-1")
    flow.subject = spike

    key = await Build._create_ticket.__wrapped__(flow, "acme/web", "Call the endpoint", "d")

    assert key == "ENG-2"
    assert created == [
        {
            "key": f"{spike.source}:ENG-1:acme/web",
            "team": "team-1",
            "label": "web",
            "title": "Call the endpoint",
            "description": "d",
        }
    ]


@pytest.mark.parametrize("plan_gate", ["human", "machine", "machine_then_human", "adaptive"])
async def test_blocked_plan_fails_the_run_before_any_review(monkeypatch, plan_gate):
    """A planner that cannot read the ticket or the repo fails the run with its
    own reason. Neither the machine reviewer nor the operator sees the plan."""
    flow = _flow(plan_gate=plan_gate)
    _fake_plans(monkeypatch, _blocked_plan("Blocked: the tracker tools cannot read ACME-1."))
    _no_review_agent(monkeypatch)

    async def fail_park(*, questions=None, context=""):
        raise AssertionError("a blocked plan must not park")

    flow.review = fail_park

    with pytest.raises(FatalError) as failure:
        await flow._plan_phase()
    assert str(failure.value) == "Blocked: the tracker tools cannot read ACME-1."


async def test_blocked_redraft_fails_the_run_instead_of_implementing(monkeypatch):
    """The machine gate sends a redraft to implementation without a second
    review, so a blocked redraft fails the run on its own pass."""
    flow = _flow(plan_gate="machine")
    passes = _fake_plans(
        monkeypatch,
        PlanData(plan_markdown="v1"),
        _blocked_plan("Blocked: the repo checkout is empty."),
    )
    _fake_grades(
        monkeypatch,
        ReviewOutput(decision=ReviewDecision.REQUEST_CHANGES, body="name the wire schema"),
    )

    with pytest.raises(FatalError, match="the repo checkout is empty"):
        await flow._plan_phase()
    assert [passing["reviewer_notes"] for passing in passes] == ["", "name the wire schema"]


async def test_review_work_ask_renders_a_notification_body(monkeypatch):
    workflow = _flow()
    workflow.subject = SimpleNamespace(repo="org/repo")
    workflow.journal = BuildJournal()
    workflow.journal.add(CiReport(head_sha="0f3a2c1d", conclusion="green"))
    monkeypatch.setattr(Build, "pr_number", property(lambda self: 42))
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
    assert (await run.get_rendered_ask())["body"]
    assert input_requests[0]["url"] == "https://github.com/org/repo/pull/42"
    assert input_requests[0]["label"] == "Review implementation on GitHub: PR #42"


@pytest.mark.parametrize(
    ("conclusion", "note"),
    [
        ("red", "CI on 0f3a2c1 is red"),
        ("unfinished", "CI on 0f3a2c1 did not finish within 60 minutes"),
    ],
)
async def test_a_round_without_green_ci_parks_saying_so(monkeypatch, conclusion, note):
    workflow = _flow()
    workflow.subject = SimpleNamespace(repo="org/repo")
    workflow.journal = BuildJournal()
    workflow.journal.add(CiReport(head_sha="0f3a2c1d", conclusion=conclusion))
    monkeypatch.setattr(Build, "pr_number", property(lambda self: 42))
    input_requests: list[dict] = []

    async def fake_wait(*, input_request):
        input_requests.append(input_request)
        return ReviewWork(action="merged")

    monkeypatch.setattr(ReviewWork, "wait", fake_wait)

    await workflow._work_gate()

    assert input_requests[0]["label"] == f"Review implementation on GitHub: PR #42. {note}"


@pytest.mark.parametrize(
    ("gate", "calls"), [("human", ["clear_draft", "merge"]), ("none", ["merge"])]
)
async def test_only_a_person_s_approval_clears_the_draft_before_a_merge(monkeypatch, gate, calls):
    flow = Build()
    flow._policy = RepoPolicy(on_approval="merge", gates={"implementation_approval": gate})
    flow.subject = SimpleNamespace(repo="org/repo")
    flow.journal = BuildJournal()
    flow.journal.add(CiReport(head_sha="0f3a2c1d", conclusion="red"))
    monkeypatch.setattr(Build, "pr_number", property(lambda self: 42))
    recorded: list[str] = []

    async def fake_wait(*, input_request):
        return ReviewWork(action="approve")

    async def clear_draft():
        recorded.append("clear_draft")

    async def merge():
        recorded.append("merge")
        return True

    monkeypatch.setattr(ReviewWork, "wait", fake_wait)
    monkeypatch.setattr(flow, "_clear_draft", clear_draft)
    monkeypatch.setattr(flow, "declare_merge_intent", merge)

    assert await flow._work_gate() is True
    assert recorded == calls


async def test_needs_clarification_delivery_stops_the_run(monkeypatch):
    """The implementer bailing (needs_clarification) fails the run with its own
    reason — the stop is a workflow decision now, not a contract side effect."""
    flow = Build()

    async def bailed():
        return SimpleNamespace(
            status="needs_clarification",
            summary="AC-3 requires a pure function that performs I/O",
        )

    monkeypatch.setattr(SoftwareFactory, "implement", bailed)
    with pytest.raises(FatalError, match="pure function"):
        await flow.implement()


async def test_approval_ends_the_build_without_merging_by_default(monkeypatch):
    """A repository that has not opted in keeps its merge: approval marks the PR
    ready and the build ends."""
    flow = Build()
    flow._policy = RepoPolicy()
    cleared = []

    async def merged():
        raise AssertionError("merge without opt-in")

    async def fake_clear_draft():
        cleared.append(True)

    monkeypatch.setattr(flow, "declare_merge_intent", merged)
    monkeypatch.setattr(flow, "_clear_draft", fake_clear_draft)

    assert await flow._approved_work() is True
    assert cleared == [True]


async def test_a_rejected_merge_intent_reparks_the_work_gate(monkeypatch):
    """GitHub declining the merge sends the operator back to the work gate,
    rather than finishing a run whose PR never merged."""
    flow = Build()
    flow._policy = RepoPolicy(on_approval="merge")
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


def _reviewing_flow(monkeypatch, facts: dict) -> tuple[Build, list[dict]]:
    flow = Build()
    flow.subject = SimpleNamespace(repo="acme/widget")
    flow.account = SimpleNamespace(id="acct-1")
    flow.journal = BuildJournal()
    monkeypatch.setattr(Build, "pr_number", property(lambda self: 42))
    dispatched: list[dict] = []

    async def dispatch(cls, **kwargs):
        dispatched.append(kwargs)

    async def wait(topic, *, label, ttl_seconds):
        assert topic == "review.completed"
        return facts

    monkeypatch.setattr(PullRequestReview, "dispatch", classmethod(dispatch))
    monkeypatch.setattr(flow, "wait", wait)
    return flow, dispatched


async def test_a_build_hands_its_pull_request_to_the_review(monkeypatch):
    flow, dispatched = _reviewing_flow(
        monkeypatch,
        {
            "decision": "request_changes",
            "summary": "Rename the flag.",
            "findings": [],
            "context_repos": [],
        },
    )

    review = await flow._review_work()

    assert dispatched == [{"repo": "acme/widget", "pr_number": 42, "account": flow.account}]
    assert review == ReviewReport(
        decision="request_changes", summary="Rename the flag.", findings=[], context_repos=[]
    )
    assert flow.journal.reviews == [review]


def _implementing_build(
    monkeypatch, *, max_revisions, heads, conclusions, decisions
) -> tuple[Build, list[str]]:
    """A build whose implementer delivers ``heads``, whose CI concludes ``conclusions``,
    and whose review decides ``decisions``, each in turn. It returns the calls made."""
    flow = Build()
    flow._settings = Build.Settings(max_implementation_revisions=max_revisions)
    flow.journal = SimpleNamespace(implementation_revision=0, last_implementation=None)
    heads, conclusions, decisions = iter(heads), iter(conclusions), iter(decisions)
    calls: list[str] = []

    async def create_branch():
        return None

    async def implement():
        delivery = SimpleNamespace(head_sha=next(heads))
        flow.journal.implementation_revision += 1
        flow.journal.last_implementation = delivery
        calls.append("implement")
        return delivery

    async def wait_for_ci(head_sha):
        conclusion = next(conclusions)
        calls.append(f"ci:{conclusion}")
        return CiReport(head_sha=head_sha, conclusion=conclusion)

    async def review_work():
        calls.append("review")
        return ReviewReport(decision=next(decisions), summary="", findings=[], context_repos=[])

    async def work_gate():
        calls.append("work_gate")
        return True

    monkeypatch.setattr(flow, "_create_branch", create_branch)
    monkeypatch.setattr(flow, "implement", implement)
    monkeypatch.setattr(flow, "_wait_for_ci", wait_for_ci)
    monkeypatch.setattr(flow, "_review_work", review_work)
    monkeypatch.setattr(flow, "_work_gate", work_gate)
    return flow, calls


@pytest.mark.parametrize(
    ("max_revisions", "expected"),
    [
        (5, ["implement", "ci:green", "review", "implement", "ci:green", "review", "work_gate"]),
        (1, ["implement", "ci:green", "review", "work_gate"]),
    ],
)
async def test_requested_changes_go_back_to_implement_until_the_cap(
    monkeypatch, max_revisions, expected
):
    flow, calls = _implementing_build(
        monkeypatch,
        max_revisions=max_revisions,
        heads=["a", "b"],
        conclusions=["green", "green"],
        decisions=["request_changes", "approve"],
    )

    await flow._implement_phase()

    assert calls == expected


@pytest.mark.parametrize(
    ("max_revisions", "expected"),
    [
        (5, ["implement", "ci:red", "implement", "ci:green", "review", "work_gate"]),
        (1, ["implement", "ci:red", "review", "work_gate"]),
    ],
)
async def test_red_ci_goes_back_to_implement_before_the_review_until_the_cap(
    monkeypatch, max_revisions, expected
):
    flow, calls = _implementing_build(
        monkeypatch,
        max_revisions=max_revisions,
        heads=["a", "b"],
        conclusions=["red", "green"],
        decisions=["approve"],
    )

    await flow._implement_phase()

    assert calls == expected


async def test_a_round_that_pushes_nothing_goes_to_the_review_without_a_ci_wait(monkeypatch):
    flow, calls = _implementing_build(
        monkeypatch, max_revisions=5, heads=["a", "a"], conclusions=["red"], decisions=["approve"]
    )

    await flow._implement_phase()

    assert calls == ["implement", "ci:red", "implement", "review", "work_gate"]


@pytest.mark.parametrize(
    ("conclusion", "decision", "leaves_draft"),
    [
        ("green", "approve", True),
        ("green", "comment", True),
        ("red", "approve", False),
        ("green", "request_changes", False),
    ],
)
async def test_the_work_park_takes_the_pr_out_of_draft_only_when_ready(
    monkeypatch, conclusion, decision, leaves_draft
):
    flow = Build()
    flow.journal.add(CiReport(head_sha="0f3a2c1d", conclusion=conclusion))
    flow.journal.add(ReviewReport(decision=decision, summary="", findings=[], context_repos=[]))
    drafts: list[bool] = []

    async def set_pr_draft(*, draft):
        drafts.append(draft)

    async def request_assignee_review():
        pass

    monkeypatch.setattr(flow, "set_pr_draft", set_pr_draft)
    monkeypatch.setattr(flow, "request_assignee_review", request_assignee_review)

    await ReviewWork.on_wait(flow)

    assert drafts == ([False] if leaves_draft else [])
