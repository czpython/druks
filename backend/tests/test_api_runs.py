from pathlib import Path

import pytest
from druks.durable.exceptions import AgentCallNotFound
from druks.durable.reads import get_agent_call_files
from druks.testing import configure_app_for_test, make_settings, seed_call, seed_run
from druks_field_notes.models import Note
from druks_field_notes.workflows import Summarize
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path: Path, druks_db, monkeypatch):
    # druks_db pre-seeds the same database that create_app opens, so we
    # just need a vanilla app — the route handlers will open their own
    # per-request Sessions against the same engine and see the seeded rows.
    # AgentCall.artifact_dir derives from load_settings().artifacts_dir;
    # point that at tmp_path so the property and the test agree on
    # where to look for run files.
    monkeypatch.setenv("DRUKS_DATA_DIR", str(tmp_path))
    settings = make_settings(tmp_path)
    app = configure_app_for_test(settings=settings)
    with TestClient(app) as client:
        yield client


def _seed_run(
    druks_db,
    *,
    tmp_path: Path,
    finished: bool = True,
    run_state: str = "running",
) -> str:
    note = Note.create(body="agent transcript")
    run = seed_run(druks_db, kind=Summarize.kind, subject=note, state=run_state)
    status = "succeeded" if finished else "running"
    call = seed_call(druks_db, run, "summarize", status=status)

    # The harness writes every file for a call into run-<run_id>/<call_id>/.
    call_dir = call.call_dir
    call_dir.mkdir(parents=True, exist_ok=True)
    (call_dir / "output.json").write_text('{"ok":true}')
    (call_dir / "metadata.json").write_text('{"pid": 1}')
    (call_dir / "stdout.jsonl").write_bytes(b"hello stdout output")
    (call_dir / "stderr.log").write_bytes(b"warning text")

    return call.id


def test_list_files_inventories_call_artifacts(
    client: TestClient,
    tmp_path: Path,
    druks_db,
):
    run_id = _seed_run(druks_db, tmp_path=tmp_path)

    response = client.get(f"/api/field_notes/transcripts/{run_id}/files")

    assert response.status_code == 200
    files = response.json()
    # The slot the file occupies is its role; each carries the file name the
    # client composes into the download URL.
    assert files["stdout"]["name"] == "stdout.jsonl"
    assert files["stderr"]["name"] == "stderr.log"
    assert files["response"]["name"] == "output.json"
    assert files["metadata"] is not None


def test_get_agent_call_files_raises_for_unknown_call(druks_db):
    with pytest.raises(AgentCallNotFound):
        get_agent_call_files("missing")


def test_transcript_unknown_call_returns_unified_404(client: TestClient, druks_db):
    response = client.get(
        "/api/field_notes/transcripts/missing",
        params={"stream": "stdout"},
    )

    assert response.status_code == 404
    assert response.json() == {
        "code": "AGENT_CALL_NOT_FOUND",
        "message": "No agent call missing.",
        "retryable": False,
    }


def test_file_listing_unknown_call_returns_unified_404(client: TestClient, druks_db):
    response = client.get("/api/field_notes/transcripts/missing/files")

    assert response.status_code == 404
    assert response.json() == {
        "code": "AGENT_CALL_NOT_FOUND",
        "message": "No agent call missing.",
        "retryable": False,
    }


def test_file_download_unknown_call_returns_unified_404(client: TestClient, druks_db):
    response = client.get("/api/field_notes/transcripts/missing/files/output.json")

    assert response.status_code == 404
    assert response.json() == {
        "code": "AGENT_CALL_NOT_FOUND",
        "message": "No agent call missing.",
        "retryable": False,
    }


def test_transcript_range_fetch_paginates(
    client: TestClient,
    tmp_path: Path,
    druks_db,
):
    run_id = _seed_run(druks_db, tmp_path=tmp_path)

    first = client.get(
        f"/api/field_notes/transcripts/{run_id}",
        params={"stream": "stdout", "limit": 5},
    )
    assert first.status_code == 200
    assert first.headers["cache-control"] == "public, max-age=31536000, immutable"
    data = first.json()
    assert data["offset"] == 0
    assert data["nextOffset"] == 5
    assert data["eof"] is False
    assert data["text"] == "hello"

    second = client.get(
        f"/api/field_notes/transcripts/{run_id}",
        params={"stream": "stdout", "offset": 5, "limit": 1024},
    )
    assert second.status_code == 200
    data = second.json()
    assert data["text"] == " stdout output"
    assert data["eof"] is True


def test_transcript_of_a_running_call_is_never_cached(
    client: TestClient,
    tmp_path: Path,
    druks_db,
):
    call_id = _seed_run(druks_db, tmp_path=tmp_path, finished=False)

    response = client.get(
        f"/api/field_notes/transcripts/{call_id}",
        params={"stream": "stdout", "limit": 5},
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"


def test_transcript_of_an_abandoned_call_is_cached_immutably(
    client: TestClient,
    tmp_path: Path,
    druks_db,
):
    # The call never wrote finished_at, but its run died — nothing will append
    # to that log again, so the chunk is as permanent as a finished call's.
    call_id = _seed_run(druks_db, tmp_path=tmp_path, finished=False, run_state="failed")

    response = client.get(
        f"/api/field_notes/transcripts/{call_id}",
        params={"stream": "stdout", "limit": 5},
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_transcript_missing_file_returns_eof(
    client: TestClient,
    tmp_path: Path,
    druks_db,
):
    note = Note.create(body="missing transcript")
    run = seed_run(druks_db, kind=Summarize.kind, subject=note)
    call = seed_call(druks_db, run, "summarize", status="running")

    response = client.get(
        f"/api/field_notes/transcripts/{call.id}",
        params={"stream": "stdout"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["eof"] is True
    assert data["text"] == ""


def test_transcript_stream_emits_chunk_then_finishes(
    client: TestClient,
    tmp_path: Path,
    druks_db,
):
    # The terminal seeded run streams its stdout in one tick: a transcript.chunk
    # carrying the log, then agent_call.finished (which ends the SSE).
    run_id = _seed_run(druks_db, tmp_path=tmp_path)

    response = client.get(
        f"/api/field_notes/transcripts/{run_id}/stream",
        params={"stream": "stdout"},
    )

    assert response.status_code == 200
    body = response.text
    assert "event: transcript.chunk" in body
    assert "hello stdout output" in body
    assert "event: agent_call.finished" in body


def test_transcript_stream_unknown_call_closes(
    client: TestClient,
    druks_db,
):
    # No such call: the stream ends instead of keepaliving forever.
    response = client.get(
        "/api/field_notes/transcripts/no-such-call/stream",
        params={"stream": "stdout"},
    )

    assert response.status_code == 200
    assert response.text == ""


def test_get_file_serves_inventory_paths(
    client: TestClient,
    tmp_path: Path,
    druks_db,
):
    run_id = _seed_run(druks_db, tmp_path=tmp_path)

    files = client.get(f"/api/field_notes/transcripts/{run_id}/files").json()
    # Compose the download URL the way the client does: the listing's own route
    # plus the file's name — the wire carries names only.
    name = files["response"]["name"]

    response = client.get(f"/api/field_notes/transcripts/{run_id}/files/{name}")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_get_file_rejects_path_traversal(
    client: TestClient,
    tmp_path: Path,
    druks_db,
):
    run_id = _seed_run(druks_db, tmp_path=tmp_path)

    response = client.get(
        f"/api/field_notes/transcripts/{run_id}/files/..%2F..%2Fetc%2Fpasswd",
    )

    assert response.status_code == 404
    assert response.json() == {
        "error": "HTTP_404",
        "detail": "File not found for this call.",
    }


def test_get_file_missing_returns_404(
    client: TestClient,
    tmp_path: Path,
    druks_db,
):
    run_id = _seed_run(druks_db, tmp_path=tmp_path)

    response = client.get(
        f"/api/field_notes/transcripts/{run_id}/files/nope.json",
    )

    assert response.status_code == 404
    assert response.json() == {
        "error": "HTTP_404",
        "detail": "File not found for this call.",
    }


def test_agent_call_artifact_layout(druks_db, tmp_path):
    # Layout sub-dir is the call id; the sandbox runner streams every run's stdout
    # to stdout.jsonl, so the layout is the same whichever harness ran the call.
    note = Note.create(body="artifact layout")
    run = seed_run(druks_db, kind=Summarize.kind, subject=note)
    call = seed_call(druks_db, run, "summarize", model="claude-opus-4-7", status="running")
    sub = Path(call.artifact_dir) / call.id
    assert call.artifact_layout.transcript == sub / "stdout.jsonl"
    assert call.artifact_layout.stderr == sub / "stderr.log"
    assert call.artifact_layout.output == sub / "output.json"
