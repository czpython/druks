import hashlib

import pytest
from druks.core.models import uuid7_str
from druks.files.models import FileRecord
from druks.files.storage import get_file_storage
from druks.models import Base
from druks.testing import asgi_client, configure_app_for_test, make_settings


@pytest.fixture(autouse=True)
def _file_storage_root(tmp_path, monkeypatch):
    monkeypatch.setenv("DRUKS_DATA_DIR", str(tmp_path))


async def _seed_file(druks_db, *, name: str, content_type: str, content: bytes) -> FileRecord:
    record = FileRecord(
        id=uuid7_str(),
        name=name,
        size=len(content),
        content_type=content_type,
        sha256=hashlib.sha256(content).hexdigest(),
        app="field_notes",
        origin_type="agent_call",
        origin_id="call-1",
    )
    druks_db.add(record)
    await druks_db.flush()
    storage = get_file_storage()
    storage.root.mkdir(parents=True, exist_ok=True)
    storage.path(record.id).write_bytes(content)
    return record


async def test_file_route_is_identity_gated(druks_db, tmp_path):
    """The files route rejects a request with no resolved operator identity."""
    api = configure_app_for_test(
        settings=make_settings(
            tmp_path,
            identity={"mode": "header", "header": "X-Identity"},
        ),
        authenticated=False,
    )

    async with asgi_client(api) as client:
        response = await client.get("/api/files/missing")

    assert response.status_code == 401


async def test_file_route_serves_inline_safe_content_with_revalidation(druks_db, druks_client):
    """Safe content serves inline with nosniff and a revalidating SHA-256 ETag."""
    record = await _seed_file(
        druks_db,
        name="screen.png",
        content_type="image/png",
        content=b"png",
    )

    response = await druks_client.get(f"/api/files/{record.id}")

    assert response.status_code == 200
    assert response.content == b"png"
    assert response.headers["content-type"] == "image/png"
    assert response.headers["content-disposition"].startswith("inline;")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["etag"] == f'"{record.sha256}"'

    unchanged = await druks_client.get(
        f"/api/files/{record.id}",
        headers={"If-None-Match": response.headers["etag"]},
    )
    assert unchanged.status_code == 304


async def test_file_route_forces_active_content_to_download(druks_db, druks_client):
    """Active same-origin content is always an attachment."""
    record = await _seed_file(
        druks_db,
        name="report.html",
        content_type="text/html",
        content=b"<script>alert(1)</script>",
    )

    response = await druks_client.get(f"/api/files/{record.id}")

    assert response.status_code == 200
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.headers["x-content-type-options"] == "nosniff"


async def test_file_route_checks_deletion_before_etag(druks_db, druks_client):
    """A matching stale ETag cannot turn a deleted file into a 304."""
    record = await _seed_file(
        druks_db,
        name="report.txt",
        content_type="text/plain",
        content=b"report",
    )
    response = await druks_client.get(f"/api/files/{record.id}")
    record.deleted_at = Base.utc_now()
    await druks_db.flush()

    deleted = await druks_client.get(
        f"/api/files/{record.id}",
        headers={"If-None-Match": response.headers["etag"]},
    )

    assert deleted.status_code == 404


async def test_file_route_returns_404_when_bytes_are_missing(druks_db, druks_client):
    """A crash-window row whose bytes are absent serves as not found."""
    record = await _seed_file(
        druks_db,
        name="missing.txt",
        content_type="text/plain",
        content=b"missing",
    )
    get_file_storage().delete(record.id)

    response = await druks_client.get(f"/api/files/{record.id}")

    assert response.status_code == 404
