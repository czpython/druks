import hashlib
import io
import shutil
import tarfile
from pathlib import Path
from urllib.parse import urlparse

from druks.core.apis.exceptions import GitHubAppNotInstalledError
from druks.core.apis.github import download_public_tarball
from druks.core.services import Github
from druks.services.exceptions import ServiceNotConnectedError

from .datastructures import CollectionContents, InstalledSkill


async def fetch_collection(
    url: str, skills_dir: Path, reserved_names: set[str]
) -> CollectionContents:
    """Fetch a GitHub repo's tarball, place every ``SKILL.md`` it contains under
    ``skills_dir/<name>/``, and return the collection's contents. Each skill's
    ``SKILL.md`` frontmatter names it. ``reserved_names`` are skill names already
    installed (by other collections) — a clash rejects the whole install, since
    the flat skills dir and its VM projection share one global name namespace."""
    repo = _parse_github_repo(url)
    try:
        github = await Github.get_client()
        archive = await github.download_tarball(repo)
    except (ServiceNotConnectedError, GitHubAppNotInstalledError):
        archive = await download_public_tarball(repo)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        members = tar.getmembers()
        if not members:
            raise ValueError("Empty repository archive")
        root = members[0].name.split("/", 1)[0]
        discovered = _discover_skills(tar, root)
        if not discovered:
            raise ValueError("Repository has no SKILL.md")
        _reject_name_clashes(discovered, reserved_names)
        installed: list[InstalledSkill] = []
        for name, description, member_root in discovered:
            target = skills_dir / name
            _extract_under_root(tar, member_root, target)
            installed.append(
                InstalledSkill(
                    name=name,
                    description=description,
                    path=str(target),
                    content_hash=_hash_tree(target),
                )
            )
    return CollectionContents(name=repo, skills=installed)


def remove_files(path: str) -> None:
    skill_dir = Path(path)
    if skill_dir.is_dir():
        shutil.rmtree(skill_dir)


def _parse_github_repo(url: str) -> str:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if parsed.netloc not in ("github.com", "www.github.com") or len(parts) < 2:
        raise ValueError(f"Not a GitHub repository URL: {url!r}")
    return f"{parts[0]}/{parts[1].removesuffix('.git')}"


def _discover_skills(tar: tarfile.TarFile, root: str) -> list[tuple[str, str, str]]:
    prefix = f"{root}/"
    skills: list[tuple[str, str, str]] = []
    for member in tar.getmembers():
        if not member.isfile() or not member.name.startswith(prefix):
            continue
        if not member.name.endswith("/SKILL.md"):
            continue
        member_root = member.name[: -len("/SKILL.md")]
        extracted = tar.extractfile(member)
        content = extracted.read() if extracted else b""
        frontmatter = _parse_frontmatter(content.decode("utf-8", "replace"))
        name = frontmatter.get("name") or member_root.rsplit("/", 1)[-1]
        _validate_name(name)
        skills.append((name, frontmatter.get("description", ""), member_root))
    return skills


def _reject_name_clashes(discovered: list[tuple[str, str, str]], reserved_names: set[str]) -> None:
    names = [name for name, _description, _root in discovered]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ValueError(f"Repository declares the same skill name twice: {', '.join(duplicates)}")
    clashes = sorted(set(names) & reserved_names)
    if clashes:
        raise ValueError(f"Skill names already installed: {', '.join(clashes)}")


def _parse_frontmatter(text: str) -> dict[str, str]:
    lines = text.splitlines()
    fields: dict[str, str] = {}
    if lines and lines[0].strip() == "---":
        for line in lines[1:]:
            if line.strip() == "---":
                break
            key, separator, value = line.partition(":")
            if separator:
                fields[key.strip()] = value.strip().strip("'\"")
    return fields


def _validate_name(name: str) -> None:
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise ValueError(f"Unsafe skill name: {name!r}")


def _extract_under_root(tar: tarfile.TarFile, member_root: str, target: Path) -> None:
    if target.exists():
        shutil.rmtree(target)
    prefix = f"{member_root}/"
    safe_root = target.resolve()
    for member in tar.getmembers():
        if not member.isfile() or not member.name.startswith(prefix):
            continue
        relative = member.name[len(prefix) :]
        if not relative:
            continue
        destination = (target / relative).resolve()
        if safe_root not in destination.parents and destination != safe_root:
            raise ValueError(f"Unsafe path in archive: {member.name}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        extracted = tar.extractfile(member)
        if extracted:
            destination.write_bytes(extracted.read())


def _hash_tree(target: Path) -> str:
    digest = hashlib.sha256()
    for file in sorted(path for path in target.rglob("*") if path.is_file()):
        digest.update(str(file.relative_to(target)).encode())
        digest.update(b"\0")
        digest.update(file.read_bytes())
    return digest.hexdigest()
