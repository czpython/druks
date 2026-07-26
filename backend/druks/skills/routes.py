from fastapi import APIRouter, Body, HTTPException
from githubkit.exception import RequestFailed, RequestTimeout

from druks.database import db_session
from druks.settings import load_settings

from .install import fetch_collection, remove_files
from .models import Skill, SkillCollection
from .schemas import CollectionResponse, SkillResponse

router = APIRouter(prefix="/api/skills", tags=["skills"])


@router.get("", response_model=list[CollectionResponse])
async def list_collections() -> list[SkillCollection]:
    return SkillCollection.list_all()


@router.post("", response_model=CollectionResponse)
async def install_collection(url: str = Body(..., embed=True)) -> SkillCollection:
    if SkillCollection.get_for_source(url):
        raise HTTPException(
            status_code=409, detail=f"Collection {url!r} already installed; remove it first."
        )
    settings = load_settings()
    try:
        contents = await fetch_collection(url, settings.skills_dir, Skill.installed_names())
    except (ValueError, RequestFailed, RequestTimeout) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except OSError as error:
        # A write failure (missing/unwritable skills_dir, disk full) is a server
        # condition, not the client's — surface it legibly instead of an opaque 500.
        raise HTTPException(
            status_code=500, detail=f"Could not write skills under {settings.skills_dir}: {error}"
        ) from error
    return SkillCollection.create(source=url, name=contents.name, skills=contents.skills)


@router.post("/{collection_id}/sync", response_model=CollectionResponse)
async def sync_collection(collection_id: str) -> SkillCollection:
    collection = SkillCollection.get(collection_id)
    if not collection:
        raise HTTPException(status_code=404, detail=f"Collection {collection_id!r} not found")
    settings = load_settings()
    current_skills = {skill.name: skill for skill in collection.skills}
    reserved_names = Skill.installed_names() - current_skills.keys()
    try:
        contents = await fetch_collection(collection.source, settings.skills_dir, reserved_names)
    except (ValueError, RequestFailed, RequestTimeout) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except OSError as error:
        raise HTTPException(
            status_code=500, detail=f"Could not write skills under {settings.skills_dir}: {error}"
        ) from error

    installed_skills = {skill.name: skill for skill in contents.skills}
    for name, skill in current_skills.items():
        installed_skill = installed_skills.pop(name, None)
        if installed_skill:
            if skill.description != installed_skill.description:
                skill.description = installed_skill.description
            if skill.content_hash != installed_skill.content_hash:
                skill.content_hash = installed_skill.content_hash
        else:
            remove_files(skill.path)
            collection.skills.remove(skill)
    for installed_skill in installed_skills.values():
        collection.skills.append(
            Skill(
                name=installed_skill.name,
                description=installed_skill.description,
                path=installed_skill.path,
                content_hash=installed_skill.content_hash,
            )
        )
    collection.updated_at = SkillCollection.utc_now()
    db_session().flush()
    return collection


@router.patch("/{collection_id}/skills/{name}", response_model=SkillResponse)
async def set_skill_enabled(
    collection_id: str,
    name: str,
    enabled: bool = Body(..., embed=True),
) -> Skill:
    skill = Skill.get(name)
    if not skill or skill.collection_id != collection_id:
        raise HTTPException(status_code=404, detail=f"Skill {name!r} not found")
    skill.enabled = enabled
    db_session().flush()
    return skill


@router.delete("/{collection_id}", status_code=204)
async def remove_collection(collection_id: str) -> None:
    collection = SkillCollection.get(collection_id)
    if not collection:
        raise HTTPException(status_code=404, detail=f"Collection {collection_id!r} not found")
    for skill in collection.skills:
        remove_files(skill.path)
    collection.delete()
