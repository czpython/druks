import pytest
from druks.api.runs import resume_run
from druks.api.schemas import ResumeRequest
from druks.durable import Run
from druks.testing import seed_dbos_status
from fastapi import HTTPException

# The shape review() parks with: an in-app ask plus the parked_at stamp the
# answer service echoes.
_ASK = {
    "presentation": "in_app",
    "controls": ["approve", "request_changes"],
    "questions": [{"id": "q1", "prompt": "which?", "options": [{"id": "a", "label": "A"}]}],
}


def _park(druks_db, *, context: str | None = None) -> None:
    ask = dict(_ASK)
    if context is not None:
        ask["context"] = context
    druks_db.add(
        Run(
            id="r1",
            kind="build",
            input_gate="review_plan",
            input_request=ask,
            input_requested_at=Run.utc_now(),
        )
    )
    druks_db.flush()
    seed_dbos_status(druks_db, "r1", "parked")


async def test_resume_sends_the_offered_control_as_the_action(druks_db, monkeypatch):
    captured: dict = {}

    async def fake_resume(self, **fields):
        captured.update(id=self.id, **fields)

    monkeypatch.setattr(Run, "resume", fake_resume)
    _park(druks_db)

    await resume_run("r1", ResumeRequest(control="approve", answers={"q1": "a"}))
    assert captured == {"id": "r1", "action": "approve", "answers": {"q1": "a"}, "note": ""}


async def test_resume_passes_free_text_answers_and_note_as_content(druks_db, monkeypatch):
    # An answer in the operator's own words and their note ride the reply verbatim —
    # content for the next plan pass, held to the asked questions but never mapped
    # to a control.
    captured: dict = {}

    async def fake_resume(self, **fields):
        captured.update(id=self.id, **fields)

    monkeypatch.setattr(Run, "resume", fake_resume)
    _park(druks_db)

    await resume_run(
        "r1",
        ResumeRequest(
            control="approve",
            answers={"q1": "neither — use the existing cache"},
            note="keep the migration out of this PR",
        ),
    )
    assert captured == {
        "id": "r1",
        "action": "approve",
        "answers": {"q1": "neither — use the existing cache"},
        "note": "keep the migration out of this PR",
    }


async def test_resume_rejects_an_unknown_control(druks_db, monkeypatch):
    # A spoofed or mislabelled control can't drive control flow — only ids the ask
    # offered map to an action.
    async def fake_resume(self, **fields):
        raise AssertionError("must not resume on a rejected control")

    monkeypatch.setattr(Run, "resume", fake_resume)
    _park(druks_db)

    with pytest.raises(HTTPException) as exc:
        await resume_run("r1", ResumeRequest(control="definitely-not-a-control"))
    assert exc.value.status_code == 422


async def test_resume_rejects_an_answer_to_a_question_that_was_not_asked(druks_db, monkeypatch):
    # Free text is only ever an answer, so it must land on a question the ask posed.
    async def fake_resume(self, **fields):
        raise AssertionError("must not resume on an invalid answer")

    monkeypatch.setattr(Run, "resume", fake_resume)
    _park(druks_db)

    with pytest.raises(HTTPException) as exc:
        await resume_run("r1", ResumeRequest(control="approve", answers={"q9": "whatever"}))
    assert exc.value.status_code == 422


@pytest.mark.parametrize("context", [None, "", "   "])
async def test_resume_rejects_request_changes_without_guidance(druks_db, monkeypatch, context):
    # request_changes exists to redirect the next pass; with no answer and a blank
    # note it would only re-run the same plan blind, so the reply is rejected.
    async def fake_resume(self, **fields):
        raise AssertionError("must not resume a guidance-free request_changes")

    monkeypatch.setattr(Run, "resume", fake_resume)
    _park(druks_db, context=context)

    with pytest.raises(HTTPException) as exc:
        await resume_run("r1", ResumeRequest(control="request_changes", note="   "))
    assert exc.value.status_code == 422
    assert exc.value.detail == "request_changes needs an answer or a note to guide the re-plan"


async def test_resume_accepts_empty_request_changes_with_ask_context(druks_db, monkeypatch):
    captured: dict = {}

    async def fake_resume(self, **fields):
        captured.update(id=self.id, **fields)

    monkeypatch.setattr(Run, "resume", fake_resume)
    _park(druks_db, context="name the rollback boundary")

    await resume_run("r1", ResumeRequest(control="request_changes", answers={}, note=""))

    assert captured == {
        "id": "r1",
        "action": "request_changes",
        "answers": {},
        "note": "",
    }


async def test_resume_request_changes_with_a_note_passes(druks_db, monkeypatch):
    captured: dict = {}

    async def fake_resume(self, **fields):
        captured.update(id=self.id, **fields)

    monkeypatch.setattr(Run, "resume", fake_resume)
    _park(druks_db)

    await resume_run("r1", ResumeRequest(control="request_changes", note="split the migration"))
    assert captured == {
        "id": "r1",
        "action": "request_changes",
        "answers": {},
        "note": "split the migration",
    }


async def test_resume_rejects_a_blank_answer(druks_db, monkeypatch):
    async def fake_resume(self, **fields):
        raise AssertionError("must not resume on a blank answer")

    monkeypatch.setattr(Run, "resume", fake_resume)
    _park(druks_db)

    with pytest.raises(HTTPException) as exc:
        await resume_run("r1", ResumeRequest(control="approve", answers={"q1": "   "}))
    assert exc.value.status_code == 422


async def test_resume_404_when_run_missing(druks_db):
    with pytest.raises(HTTPException) as exc:
        await resume_run("nope", ResumeRequest(control="approve"))
    assert exc.value.status_code == 404


async def test_resume_409_when_run_not_parked(druks_db):
    druks_db.add(Run(id="r2", kind="build"))
    druks_db.flush()
    seed_dbos_status(druks_db, "r2", "running")
    with pytest.raises(HTTPException) as exc:
        await resume_run("r2", ResumeRequest(control="approve"))
    assert exc.value.status_code == 409
