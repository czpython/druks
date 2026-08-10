# Every build output contract must produce an OpenAI strict schema
# (additionalProperties: false + every property required on every object node)
# or the Codex harness 400s at runtime. The fake-harness tests never send the
# real schema, so this guards it directly.
import pytest
from druks.contrib.ship import contracts as O
from druks.contrib.ship.enums import ReviewDecision
from druks.workflows import OperatorReply
from pydantic import ValidationError

MODELS = [
    O.PlanOutput,
    O.RepoProfilerOutput,
    O.ReviewOutput,
    O.TriageOutput,
    O.ImplementationOutput,
    O.EvaluationOutput,
    O.ContractRevisionOutput,
]


def _object_nodes(node, defs):
    if "$ref" in node:
        node = defs[node["$ref"].split("/")[-1]]
    if "properties" in node:
        yield node
        for prop in node["properties"].values():
            yield from _object_nodes(prop, defs)
    if "items" in node:
        yield from _object_nodes(node["items"], defs)
    for combinator in ("anyOf", "allOf", "oneOf"):
        for sub in node.get(combinator, []):
            yield from _object_nodes(sub, defs)


@pytest.mark.parametrize("model", MODELS, ids=lambda m: m.__name__)
def test_output_contract_is_strict(model):
    schema = model.model_json_schema()
    defs = schema.get("$defs", {})
    for node in _object_nodes(schema, defs):
        props = set(node["properties"])
        assert node.get("additionalProperties") is False, (
            f"{model.__name__}: an object node lacks additionalProperties: false"
        )
        missing = props - set(node.get("required", []))
        assert not missing, f"{model.__name__}: non-required properties {missing} break strict mode"


def test_repo_profiler_output_maps_verification_commands_to_plain_dicts():
    output = O.RepoProfilerOutput.model_validate(
        {
            "languages": ["python"],
            "frameworks": ["django"],
            "package_managers": ["uv"],
            "stack_summary": "A Django backend.",
            "test_commands": [{"command": "pytest", "ci_check": "Backend / tests"}],
            "lint_commands": [{"command": "ruff check .", "ci_check": None}],
            "typecheck_commands": [],
            "recommended_skills": ["django-patterns"],
        }
    )

    assert output.to_result() == {
        "languages": ["python"],
        "frameworks": ["django"],
        "package_managers": ["uv"],
        "stack_summary": "A Django backend.",
        "verification": {
            "test_commands": [{"command": "pytest", "ci_check": "Backend / tests"}],
            "lint_commands": [{"command": "ruff check .", "ci_check": None}],
            "typecheck_commands": [],
        },
        "recommended_skills": ["django-patterns"],
    }


def _implementation(**overrides):
    fields = {
        "type": "result",
        "status": "success",
        "base_sha": "a",
        "head_sha": "b",
        "commit_sha": "b",
        "branch": "agent/eng-1",
        "pr_number": 7,
        "files_changed": [],
        "acceptance_results": [],
        "checks": [],
        "known_risks": [],
        "summary": "",
        "workspace_path": "/repo",
        "workspace_retention": None,
    }
    fields.update(overrides)
    return O.ImplementationOutput.model_validate(fields)


def test_implementation_success_requires_a_delivery():
    # success = a pushed commit on a PR; a success without them is the fabrication
    # path (the original setup bug) and must fail at the contract, loudly.
    with pytest.raises(ValueError, match="pr_number"):
        _implementation(branch=None, pr_number=None)


def test_needs_clarification_may_omit_the_delivery_fields():
    # The bail path carries no commit — the validator binds a delivery only on
    # success, so a needs_clarification output validates with the shas left null.
    # The workflow turns that bail into a run-stopping failure (see the plan-phase tests).
    bailed = _implementation(
        status="needs_clarification",
        summary="AC-3 requires a pure function that performs I/O",
        base_sha=None,
        head_sha=None,
        commit_sha=None,
        branch=None,
        pr_number=None,
    )
    assert bailed.status == "needs_clarification"
    assert "pure function" in bailed.summary


def test_answered_questions_map_picks_to_labels_and_keep_free_text_verbatim():
    # An answer is an offered option id (paired as its label) or the operator's own
    # words (kept verbatim); unanswered questions don't reach the re-plan agent.
    plan = O.PlanData(
        questions=[
            O.QuestionOutput(
                id="q1",
                prompt="Which cache?",
                options=[O.QuestionOptionOutput(id="a", label="Redis", recommended=False)],
            ),
            O.QuestionOutput(
                id="q2",
                prompt="Which queue?",
                options=[O.QuestionOptionOutput(id="a", label="SQS", recommended=False)],
            ),
            O.QuestionOutput(id="q3", prompt="Feature flag?", options=[]),
        ]
    )
    assert plan.get_answered_questions({"q1": "a", "q2": "kafka — we already run it"}) == [
        {"question": "Which cache?", "answer": "Redis"},
        {"question": "Which queue?", "answer": "kafka — we already run it"},
    ]


def test_agreement_needs_every_question_picked_as_recommended():
    confirmed = O.PlanData(
        questions=[
            O.QuestionOutput(
                id="q1",
                prompt="Which cache?",
                options=[O.QuestionOptionOutput(id="a", label="Redis", recommended=True)],
            ),
            O.QuestionOutput(
                id="q2",
                prompt="Which queue?",
                options=[O.QuestionOptionOutput(id="b", label="SQS", recommended=True)],
            ),
        ]
    )
    without_recommendation = O.PlanData(
        questions=[
            O.QuestionOutput(
                id="q1",
                prompt="Which cache?",
                options=[O.QuestionOptionOutput(id="a", label="Redis", recommended=False)],
            )
        ]
    )

    assert confirmed.uses_recommended_answers({"q1": "a", "q2": "b"})
    assert not confirmed.uses_recommended_answers({"q1": "a"})
    assert not without_recommendation.uses_recommended_answers({"q1": "a"})
    assert O.PlanData().uses_recommended_answers({})


def test_confirmation_needs_acceptance_criteria_and_an_unchanged_approval():
    criteria = [
        O.AcceptanceCriterionOutput(id="AC-1", description="It ships.", verification="Read it.")
    ]
    complete = O.PlanData(acceptance_criteria=criteria)

    assert complete.is_confirmed_by(OperatorReply(action="approve"))
    assert not complete.is_confirmed_by(OperatorReply(action="approve", note="one more thing"))
    assert not complete.is_confirmed_by(OperatorReply(action="request_changes"))
    # An empty plan is never confirmed, however the operator answered it.
    assert not O.PlanData().is_confirmed_by(OperatorReply(action="approve"))


def test_ask_contracts_cap_identity_and_cardinality():
    # The gate view is bounded by construction: identity and list sizes are
    # hard caps at the agent boundary, never clipped downstream.
    option = O.QuestionOptionOutput(id="a", label="Redis", recommended=False)
    with pytest.raises(ValidationError):
        O.QuestionOptionOutput(id="a" * 65, label="Redis", recommended=False)
    with pytest.raises(ValidationError):
        O.QuestionOutput(id="q", prompt="p" * 2049, options=[])
    with pytest.raises(ValidationError):
        O.QuestionOutput(id="q", prompt="p", options=[option] * 17)
    with pytest.raises(ValidationError):
        O.PlanOutput(
            plan_markdown="m",
            acceptance_criteria=[],
            questions=[O.QuestionOutput(id=f"q{i}", prompt="p", options=[]) for i in range(9)],
            assignee_github_login=None,
        )


def test_review_output_records_no_artifact():
    # An artifact would displace the plan as the parked ask's document.
    grade = O.ReviewOutput(decision=ReviewDecision.REQUEST_CHANGES, body="name the wire schema")
    assert grade.get_artifact() == {}


@pytest.mark.parametrize("body", ["", "   \n"])
def test_request_changes_requires_a_critique_body(body):
    # The critique is the redraft's only guidance; empty would redraft blind.
    with pytest.raises(ValidationError, match="critique"):
        O.ReviewOutput(decision=ReviewDecision.REQUEST_CHANGES, body=body)
    assert O.ReviewOutput(decision=ReviewDecision.APPROVE, body=body).body == body
