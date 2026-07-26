import io
import tarfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import configure_app_for_test, make_settings
from druks.skills import install as install_mod
from druks.skills import routes as routes_mod
from druks.skills.datastructures import CollectionContents, InstalledSkill
from druks.skills.models import Skill, SkillCollection
from fastapi.testclient import TestClient


def _tarball(root: str, files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for relative, content in files.items():
            info = tarfile.TarInfo(f"{root}/{relative}")
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def _patch_download(monkeypatch, archive: bytes) -> None:
    async def fake_download(owner, repo, ref=""):
        return archive

    monkeypatch.setattr(install_mod, "download_public_tarball", fake_download)


def _skill_md(name: str, description: str = "") -> bytes:
    description = description or f"the {name} skill"
    return f"---\nname: {name}\ndescription: {description}\n---\n# {name}\nbody\n".encode()


async def _fetch(url: str, skills_dir: Path, reserved: set[str] | None = None):
    return await install_mod.fetch_collection(url, skills_dir, reserved or set())


def _install_collection(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    files: dict[str, bytes],
) -> str:
    _patch_download(monkeypatch, _tarball("owner-repo-abc", files))
    response = client.post("/api/skills", json={"url": "https://github.com/owner/repo"})
    assert response.status_code == 200
    return response.json()["id"]


async def test_fetch_collection_lands_every_skill(tmp_path, monkeypatch):
    _patch_download(
        monkeypatch,
        _tarball(
            "owner-repo-abc",
            {
                "alpha/SKILL.md": _skill_md("alpha"),
                "alpha/ref/r.md": b"ref",
                "nested/beta/SKILL.md": _skill_md("beta"),
            },
        ),
    )
    contents = await _fetch("https://github.com/owner/repo", tmp_path)

    assert contents.name == "owner/repo"
    assert sorted(skill.name for skill in contents.skills) == ["alpha", "beta"]
    assert (tmp_path / "alpha" / "SKILL.md").read_bytes() == _skill_md("alpha")
    assert (tmp_path / "alpha" / "ref" / "r.md").read_bytes() == b"ref"
    assert (tmp_path / "beta" / "SKILL.md").read_bytes() == _skill_md("beta")


async def test_fetch_collection_root_skill_is_a_collection_of_one(tmp_path, monkeypatch):
    _patch_download(monkeypatch, _tarball("owner-repo-abc", {"SKILL.md": _skill_md("solo")}))
    contents = await _fetch("https://github.com/owner/repo", tmp_path)

    assert [skill.name for skill in contents.skills] == ["solo"]
    assert (tmp_path / "solo" / "SKILL.md").read_bytes() == _skill_md("solo")


async def test_fetch_collection_rejects_missing_skill_md(tmp_path, monkeypatch):
    _patch_download(monkeypatch, _tarball("owner-repo-abc", {"README.md": b"no skill"}))
    with pytest.raises(ValueError, match="SKILL.md"):
        await _fetch("https://github.com/owner/repo", tmp_path)


async def test_fetch_collection_rejects_reserved_name(tmp_path, monkeypatch):
    _patch_download(monkeypatch, _tarball("owner-repo-abc", {"alpha/SKILL.md": _skill_md("alpha")}))
    with pytest.raises(ValueError, match="already installed"):
        await _fetch("https://github.com/owner/repo", tmp_path, reserved={"alpha"})


async def test_fetch_collection_rejects_path_traversal(tmp_path, monkeypatch):
    _patch_download(
        monkeypatch,
        _tarball("root", {"alpha/SKILL.md": _skill_md("alpha"), "alpha/../escape.txt": b"evil"}),
    )
    with pytest.raises(ValueError, match="Unsafe path"):
        await _fetch("https://github.com/owner/repo", tmp_path)


def test_non_github_url_rejected():
    with pytest.raises(ValueError, match="GitHub"):
        install_mod._parse_github_repo("https://gitlab.com/owner/repo")


def test_collection_create_get_cascade_delete(db_session):
    collection = SkillCollection.create(
        source="https://github.com/o/r",
        name="o/r",
        skills=[
            InstalledSkill(name="alpha", description="one", path="/p/alpha", content_hash="a"),
            InstalledSkill(name="beta", description="two", path="/p/beta", content_hash="b"),
        ],
    )
    assert SkillCollection.get_for_source("https://github.com/o/r").id == collection.id
    assert Skill.installed_names() == {"alpha", "beta"}
    assert [c.name for c in SkillCollection.list_all()] == ["o/r"]

    # Skills land enabled; disabling drops them from the projection's tar.
    assert Skill.disabled_excludes() == ()
    Skill.get("alpha").enabled = False
    db_session.flush()
    assert Skill.disabled_excludes() == ("./alpha",)

    collection.delete()
    assert SkillCollection.list_all() == []
    assert Skill.installed_names() == set()


def test_collection_routes_install_list_remove(tmp_path, monkeypatch):
    async def fake_fetch(url, skills_dir, reserved_names):
        return CollectionContents(
            name="o/r",
            skills=[
                InstalledSkill(
                    name="alpha",
                    description="one",
                    path=str(Path(skills_dir) / "alpha"),
                    content_hash="a",
                )
            ],
        )

    monkeypatch.setattr(routes_mod, "fetch_collection", fake_fetch)
    with TestClient(configure_app_for_test(settings=make_settings(tmp_path))) as client:
        created = client.post("/api/skills", json={"url": "https://github.com/o/r"})
        assert created.status_code == 200
        body = created.json()
        assert body["name"] == "o/r"
        skill = body["skills"][0]
        assert skill["name"] == "alpha"
        assert skill["description"] == "one"
        assert skill["enabled"] is True
        assert "updatedAt" in skill
        collection_id = body["id"]

        # Disable the skill — flips enabled, kept in the collection.
        toggled = client.patch(f"/api/skills/{collection_id}/skills/alpha", json={"enabled": False})
        assert toggled.status_code == 200
        assert toggled.json()["enabled"] is False
        listed_skill = client.get("/api/skills").json()[0]["skills"][0]
        assert listed_skill["enabled"] is False

        # Re-installing the same source is rejected — remove first.
        assert client.post("/api/skills", json={"url": "https://github.com/o/r"}).status_code == 409

        assert [c["name"] for c in client.get("/api/skills").json()] == ["o/r"]

        assert client.delete(f"/api/skills/{collection_id}").status_code == 204
        assert client.get("/api/skills").json() == []


def test_sync_updates_changed_skill_and_timestamps(tmp_path, monkeypatch, db_session):
    settings = make_settings(tmp_path)
    original_skill = _skill_md("alpha")
    updated_skill = _skill_md("alpha", "updated description")
    old_timestamp = datetime(2020, 1, 1, tzinfo=UTC)

    with TestClient(configure_app_for_test(settings=settings)) as client:
        collection_id = _install_collection(
            client,
            monkeypatch,
            {"alpha/SKILL.md": original_skill},
        )
        collection = SkillCollection.get(collection_id)
        skill = Skill.get("alpha")
        original_hash = skill.content_hash
        collection.updated_at = old_timestamp
        skill.updated_at = old_timestamp
        db_session.flush()

        _patch_download(
            monkeypatch,
            _tarball("owner-repo-abc", {"alpha/SKILL.md": updated_skill}),
        )
        response = client.post(f"/api/skills/{collection_id}/sync")
        assert response.status_code == 200
        assert response.json()["skills"][0]["description"] == "updated description"

        db_session.expire_all()
        assert skill.description == "updated description"
        assert skill.content_hash != original_hash
        assert skill.updated_at > old_timestamp
        assert collection.updated_at > old_timestamp
        assert (settings.skills_dir / "alpha" / "SKILL.md").read_bytes() == updated_skill

        skill_timestamp = skill.updated_at
        collection.updated_at = old_timestamp
        db_session.flush()
        response = client.post(f"/api/skills/{collection_id}/sync")
        assert response.status_code == 200

        db_session.expire_all()
        assert skill.updated_at == skill_timestamp
        assert collection.updated_at > old_timestamp


def test_sync_adds_new_skill(tmp_path, monkeypatch, db_session):
    settings = make_settings(tmp_path)
    alpha_skill = _skill_md("alpha")
    beta_skill = _skill_md("beta")

    with TestClient(configure_app_for_test(settings=settings)) as client:
        collection_id = _install_collection(
            client,
            monkeypatch,
            {"alpha/SKILL.md": alpha_skill},
        )
        _patch_download(
            monkeypatch,
            _tarball(
                "owner-repo-abc",
                {
                    "alpha/SKILL.md": alpha_skill,
                    "beta/SKILL.md": beta_skill,
                },
            ),
        )

        response = client.post(f"/api/skills/{collection_id}/sync")
        assert response.status_code == 200
        assert [skill["name"] for skill in response.json()["skills"]] == ["alpha", "beta"]

        db_session.expire_all()
        assert Skill.get("beta").collection_id == collection_id
        assert (settings.skills_dir / "beta" / "SKILL.md").read_bytes() == beta_skill


def test_sync_removes_missing_skill_and_files(tmp_path, monkeypatch, db_session):
    settings = make_settings(tmp_path)
    alpha_skill = _skill_md("alpha")
    beta_skill = _skill_md("beta")

    with TestClient(configure_app_for_test(settings=settings)) as client:
        collection_id = _install_collection(
            client,
            monkeypatch,
            {
                "alpha/SKILL.md": alpha_skill,
                "beta/SKILL.md": beta_skill,
            },
        )
        beta_path = settings.skills_dir / "beta"
        assert beta_path.is_dir()
        _patch_download(
            monkeypatch,
            _tarball("owner-repo-abc", {"alpha/SKILL.md": alpha_skill}),
        )

        response = client.post(f"/api/skills/{collection_id}/sync")
        assert response.status_code == 200
        assert [skill["name"] for skill in response.json()["skills"]] == ["alpha"]

        db_session.expire_all()
        assert not Skill.get("beta")
        assert not beta_path.exists()


def test_sync_preserves_disabled_skill(tmp_path, monkeypatch, db_session):
    alpha_skill = _skill_md("alpha")

    with TestClient(configure_app_for_test(settings=make_settings(tmp_path))) as client:
        collection_id = _install_collection(
            client,
            monkeypatch,
            {"alpha/SKILL.md": alpha_skill},
        )
        skill = Skill.get("alpha")
        skill.enabled = False
        db_session.flush()
        _patch_download(
            monkeypatch,
            _tarball("owner-repo-abc", {"alpha/SKILL.md": alpha_skill}),
        )

        response = client.post(f"/api/skills/{collection_id}/sync")
        assert response.status_code == 200
        assert response.json()["skills"][0]["enabled"] is False

        db_session.expire_all()
        assert skill.enabled is False


def test_sync_nonexistent_collection_returns_404(tmp_path):
    with TestClient(configure_app_for_test(settings=make_settings(tmp_path))) as client:
        response = client.post("/api/skills/unknown/sync")

    assert response.status_code == 404
    assert response.json()["detail"] == "Collection 'unknown' not found"


def test_sync_allows_collection_own_existing_skill_name(tmp_path, monkeypatch):
    alpha_skill = _skill_md("alpha")

    with TestClient(configure_app_for_test(settings=make_settings(tmp_path))) as client:
        collection_id = _install_collection(
            client,
            monkeypatch,
            {"alpha/SKILL.md": alpha_skill},
        )
        _patch_download(
            monkeypatch,
            _tarball("owner-repo-abc", {"alpha/SKILL.md": alpha_skill}),
        )

        response = client.post(f"/api/skills/{collection_id}/sync")

    assert response.status_code == 200
    assert [skill["name"] for skill in response.json()["skills"]] == ["alpha"]
