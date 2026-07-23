# Every build output contract must produce an OpenAI strict schema
# (additionalProperties: false + every property required on every object node)
# or the Codex harness 400s at runtime. The fake-harness tests never send the
# real schema, so this guards it directly.
import pytest
from druks.build import contracts as O
from pydantic import ValidationError

MODELS = [
    O.PlanOutput,
    O.RepoProfilerOutput,
    O.ReviewOutput,
    O.TriageOutput,
    O.ImplementationOutput,
    O.EvaluationOutput,
    O.CodeReviewOutput,
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


def test_get_answered_maps_picks_to_labels_and_keeps_free_text_verbatim():
    # An answer is an offered option id (paired as its label) or the operator's own
    # words (kept verbatim); unanswered questions don't reach the re-plan agent.
    plan = O.PlanData(
        questions=[
            O.QuestionOutput(
                id="q1",
                prompt="Which cache?",
                options=[O.QuestionOptionOutput(id="a", label="Redis")],
            ),
            O.QuestionOutput(
                id="q2",
                prompt="Which queue?",
                options=[O.QuestionOptionOutput(id="a", label="SQS")],
            ),
            O.QuestionOutput(id="q3", prompt="Feature flag?", options=[]),
        ]
    )
    assert plan.get_answered({"q1": "a", "q2": "kafka — we already run it"}) == [
        {"question": "Which cache?", "answer": "Redis"},
        {"question": "Which queue?", "answer": "kafka — we already run it"},
    ]


def test_ask_contracts_cap_identity_and_cardinality():
    # The gate view is bounded by construction: identity and list sizes are
    # hard caps at the agent boundary, never clipped downstream.
    option = O.QuestionOptionOutput(id="a", label="Redis")
    with pytest.raises(ValidationError):
        O.QuestionOptionOutput(id="a" * 65, label="Redis")
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
    from druks.build.enums import ReviewDecision

    grade = O.ReviewOutput(decision=ReviewDecision.REQUEST_CHANGES, body="name the wire schema")
    assert grade.get_artifact() == {}
