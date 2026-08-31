import hashlib
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from druks.accounts.models import Account
from druks.agents import AgentOutput
from druks.durable import AgentCall
from druks.files import File, FileField
from druks.files.exceptions import FileTooLargeError, FileUnavailableError
from druks.files.models import FileRecord
from druks.files.storage import LocalFileStorage, reap_deleted_file_bytes
from druks.models import Base
from druks.sandbox.exceptions import SandboxDownloadError
from druks.testing import seed_call, seed_run
from druks.workspaces import Workspace
from sqlalchemy import Integer, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column


class SurveyShot(AgentOutput):
    screen: str
    image: File


class SurveyOutput(AgentOutput):
    shots: list[SurveyShot]


class FileReference(Base):
    __tablename__ = "test_file_references"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    image: Mapped[File] = FileField()


async def _survey_call(session) -> AgentCall:
    return await seed_call(session, await seed_run(session, kind="survey"), agent="survey")


async def _hydrate_file(
    session, tmp_path, monkeypatch, *, name="home.png", content=b"image"
) -> File:
    monkeypatch.setenv("DRUKS_DATA_DIR", str(tmp_path))
    workspace_files: list[File] = []
    output = SurveyOutput.model_validate(
        {"shots": [{"screen": "home", "image": f"shots/{name}"}]},
        context={"workspace_files": workspace_files},
    )
    host = MagicMock(ssh_username="root")

    async def download(*, remote, local, workspace_root, max_bytes):
        local.write_bytes(content)

    host.download = AsyncMock(side_effect=download)
    call = await _survey_call(session)
    await Workspace(host=host).save_files(
        workspace_files,
        app="field_notes",
        agent_call_id=call.id,
    )
    return output.shots[0].image


async def test_create_stores_an_upload_against_its_account(druks_db, tmp_path, monkeypatch):
    """File.create stores bytes against the account that sent them."""
    monkeypatch.setenv("DRUKS_DATA_DIR", str(tmp_path))

    file = await File.create(
        name="shopfront.jpg",
        content_type="image/jpeg",
        content=b"jpeg bytes",
        app="site_builder",
        uploaded_by="system",
    )

    record = await druks_db.get(FileRecord, file.id)
    assert (file.name, file.size, file.content_type, file.url) == (
        "shopfront.jpg",
        10,
        "image/jpeg",
        f"/api/files/{file.id}",
    )
    assert (record.app, record.uploaded_by, record.agent_call_id) == (
        "site_builder",
        "system",
        None,
    )
    assert record.sha256 == hashlib.sha256(b"jpeg bytes").hexdigest()
    assert await file.open() == b"jpeg bytes"


async def test_create_refuses_an_oversized_upload(monkeypatch):
    """An upload past the byte cap raises before any byte or row lands."""
    monkeypatch.setattr("druks.files.datastructures.MAX_FILE_BYTES", 4)

    with pytest.raises(FileTooLargeError, match="cap"):
        await File.create(
            name="shopfront.jpg",
            content_type="image/jpeg",
            content=b"jpeg bytes",
            app="site_builder",
            uploaded_by="system",
        )


async def test_uploaded_file_delete_and_reap(druks_db, tmp_path, monkeypatch):
    """A deleted upload loses its bytes to the reaper and keeps its tombstone."""
    monkeypatch.setenv("DRUKS_DATA_DIR", str(tmp_path))
    file = await File.create(
        name="shopfront.jpg",
        content_type="image/jpeg",
        content=b"jpeg bytes",
        app="site_builder",
        uploaded_by="system",
    )
    await file.delete()
    record = await druks_db.get(FileRecord, file.id)
    record.deleted_at = Base.utc_now() - timedelta(days=2)
    await druks_db.flush()

    assert await reap_deleted_file_bytes() == 1
    assert await druks_db.get(FileRecord, file.id) is record
    assert not LocalFileStorage(tmp_path / "files").path(file.id).exists()


def test_file_contract_is_a_strict_nested_workspace_path():
    """A nested File stays a strict string-path node in the harness schema."""
    schema = SurveyOutput.model_json_schema()

    assert schema["additionalProperties"] is False
    assert schema["$defs"]["SurveyShot"]["additionalProperties"] is False
    assert schema["$defs"]["SurveyShot"]["properties"]["image"]["type"] == "string"


async def test_hydration_pulls_and_stores_a_nested_file(druks_db, tmp_path, monkeypatch):
    """Hydration stores immutable bytes and stamps the handle and provenance."""
    file = await _hydrate_file(druks_db, tmp_path, monkeypatch, content=b"png bytes")

    record = await druks_db.get(FileRecord, file.id)
    assert (file.name, file.size, file.content_type, file.url) == (
        "home.png",
        9,
        "image/png",
        f"/api/files/{file.id}",
    )
    assert record.app == "field_notes"
    assert await druks_db.get(AgentCall, record.agent_call_id)
    assert not record.uploaded_by
    assert record.sha256 == hashlib.sha256(b"png bytes").hexdigest()
    assert await file.open() == b"png bytes"


async def test_a_deleted_agent_call_leaves_its_file_without_a_source(
    druks_db, tmp_path, monkeypatch
):
    """A file outlives the call that made it, and stops naming it."""
    file = await _hydrate_file(druks_db, tmp_path, monkeypatch)
    record = await druks_db.get(FileRecord, file.id)

    await druks_db.delete(await druks_db.get(AgentCall, record.agent_call_id))
    await druks_db.flush()
    await druks_db.refresh(record)

    assert not record.agent_call_id
    assert await file.open() == b"image"


async def test_an_upload_holds_its_uploader(druks_db, tmp_path, monkeypatch):
    """An upload holds its uploader, so the account cannot leave without it."""
    monkeypatch.setenv("DRUKS_DATA_DIR", str(tmp_path))
    account = Account(username="shopkeeper@example.com")
    druks_db.add(account)
    await druks_db.flush()
    await File.create(
        name="shopfront.jpg",
        content_type="image/jpeg",
        content=b"jpeg bytes",
        app="site_builder",
        uploaded_by=account.id,
    )

    await druks_db.delete(account)
    with pytest.raises(IntegrityError):
        await druks_db.flush()


async def test_hydration_leaves_no_canonical_bytes_when_a_later_pull_fails(
    druks_db, tmp_path, monkeypatch
):
    """A failed batch removes every staged byte before any canonical file exists."""
    monkeypatch.setenv("DRUKS_DATA_DIR", str(tmp_path))
    workspace_files: list[File] = []
    SurveyOutput.model_validate(
        {
            "shots": [
                {"screen": "home", "image": "shots/home.png"},
                {"screen": "settings", "image": "shots/missing.png"},
            ]
        },
        context={"workspace_files": workspace_files},
    )
    host = MagicMock(ssh_username="root")

    async def download(*, remote, local, workspace_root, max_bytes):
        if remote.endswith("home.png"):
            local.write_bytes(b"first")
            return
        raise SandboxDownloadError("reported file is missing: shots/missing.png")

    host.download = AsyncMock(side_effect=download)

    call = await _survey_call(druks_db)
    with pytest.raises(SandboxDownloadError, match="missing"):
        await Workspace(host=host).save_files(
            workspace_files,
            app="field_notes",
            agent_call_id=call.id,
        )

    files_dir = tmp_path / "files"
    assert list(files_dir.iterdir()) == []


async def test_prepared_context_uploads_into_the_call_directory(druks_db, tmp_path, monkeypatch):
    """A hydrated handle becomes a path inside the call's own directory."""
    file = await _hydrate_file(druks_db, tmp_path, monkeypatch)
    host = MagicMock(ssh_username="root")
    host.upload_file = AsyncMock()

    context = await Workspace(host=host).prepare_context({"source": file}, agent_call_id="call-2")

    assert context == {"source": f"/root/work/.druks-files/call-2/{file.id}/home.png"}
    host.upload_file.assert_awaited_once_with(
        local=LocalFileStorage(tmp_path / "files").path(file.id),
        remote=context["source"],
    )


async def test_prepare_context_refuses_a_deleted_file(druks_db, tmp_path, monkeypatch):
    """A deleted handle never uploads bytes into the next agent call."""
    file = await _hydrate_file(druks_db, tmp_path, monkeypatch)
    await file.delete()
    host = MagicMock(ssh_username="root")
    host.upload_file = AsyncMock()

    with pytest.raises(FileUnavailableError, match="deleted or missing"):
        await Workspace(host=host).prepare_context({"source": file}, agent_call_id="call-2")

    host.upload_file.assert_not_awaited()


async def test_file_field_round_trip_loads_metadata_with_the_row(druks_db, tmp_path, monkeypatch):
    """FileField persists a real FK and projects complete handle metadata on load."""
    file = await _hydrate_file(druks_db, tmp_path, monkeypatch, content=b"1234")
    reference = FileReference(image=file)
    druks_db.add(reference)
    await druks_db.flush()
    reference_id = reference.id
    druks_db.expunge(reference)

    loaded = await druks_db.scalar(select(FileReference).where(FileReference.id == reference_id))
    loaded_file = loaded.image
    druks_db.expunge(loaded)

    assert (loaded_file.id, loaded_file.url) == (file.id, f"/api/files/{file.id}")
    assert (loaded_file.name, loaded_file.size, loaded_file.content_type) == (
        "home.png",
        4,
        "image/png",
    )
    [foreign_key] = FileReference.__table__.c.image.foreign_keys
    assert foreign_key.target_fullname == "files.id"


async def test_delete_and_reaper_leave_the_tombstone(druks_db, tmp_path, monkeypatch):
    """The reaper removes bytes while the soft-deleted row and app FK remain."""
    file = await _hydrate_file(druks_db, tmp_path, monkeypatch)
    reference = FileReference(image=file)
    druks_db.add(reference)
    await druks_db.flush()
    await file.delete()
    record = await druks_db.get(FileRecord, file.id)
    record.deleted_at = Base.utc_now() - timedelta(days=2)
    await druks_db.flush()

    assert await reap_deleted_file_bytes() == 1
    assert await druks_db.get(FileRecord, file.id) is record
    assert reference.image.id == file.id
    assert not LocalFileStorage(tmp_path / "files").path(file.id).exists()
    with pytest.raises(FileUnavailableError, match="deleted or missing"):
        await file.open()
