import httpx
import pytest
from druks.files.constants import MAX_UPLOAD_BYTES
from druks.files.models import FileRecord
from druks.testing import asgi_client, configure_app_for_test, make_settings
from sqlalchemy import func, select


@pytest.fixture(autouse=True)
def _file_storage_root(tmp_path, monkeypatch):
    monkeypatch.setenv("DRUKS_DATA_DIR", str(tmp_path))


async def test_an_upload_lands_under_the_app_that_asked_for_it(
    druks_client: httpx.AsyncClient, druks_db
):
    """The file belongs to the app whose page holds the form, and to the operator."""
    answer = await druks_client.post(
        "/api/field_notes/uploads",
        files={"file": ("shopfront.jpg", b"jpeg bytes", "image/jpeg")},
    )

    assert answer.status_code == 200
    body = answer.json()
    assert (body["name"], body["contentType"], body["size"]) == ("shopfront.jpg", "image/jpeg", 10)
    assert body["url"] == f"/api/files/{body['id']}"
    record = await druks_db.get(FileRecord, body["id"])
    assert record.app == "field_notes"
    # The uploader is the caller the identity gate resolved, never the client's word.
    assert record.uploaded_by
    assert not record.agent_call_id


async def test_the_name_types_the_file_whatever_the_browser_claims(
    druks_client: httpx.AsyncClient, druks_db
):
    """One source for the media type: the file's own name."""
    answer = await druks_client.post(
        "/api/field_notes/uploads",
        files={"file": ("notes.txt", b"plain", "application/x-lying")},
    )

    assert answer.json()["contentType"] == "text/plain"


async def test_an_oversized_file_is_refused_and_nothing_lands(
    druks_client: httpx.AsyncClient, druks_db
):
    """The cap is the platform's, and it answers before any byte is stored."""
    answer = await druks_client.post(
        "/api/field_notes/uploads",
        files={"file": ("huge.bin", b"x" * (MAX_UPLOAD_BYTES + 1), "application/octet-stream")},
    )

    assert answer.status_code == 413
    assert "Choose a smaller one" in answer.json()["detail"]
    assert await druks_db.scalar(select(func.count()).select_from(FileRecord)) == 0


async def test_the_upload_route_is_identity_gated(druks_db, tmp_path):
    """Bytes never land without an operator to attribute them to."""
    api = configure_app_for_test(
        settings=make_settings(tmp_path, identity={"mode": "header", "header": "X-Identity"}),
        authenticated=False,
    )

    async with asgi_client(api) as client:
        answer = await client.post(
            "/api/field_notes/uploads", files={"file": ("a.txt", b"a", "text/plain")}
        )

    assert answer.status_code == 401
