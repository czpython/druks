import contextlib
import time

from druks.core.apis.github import get_github_client
from druks.settings import load_settings

_TTL_SECONDS = 60 * 60 * 24  # 24 hours
_CACHE_DIR = "druks-cache"


async def fetch_file(*, repo: str, path: str) -> str | None:
    """``path``'s body from ``repo``'s default branch, or ``None`` for a
    404. Served from the disk cache while fresh; a cached empty file is a
    remembered 404."""
    cache = load_settings().data_dir / _CACHE_DIR / repo / path
    with contextlib.suppress(FileNotFoundError):
        if time.time() - cache.stat().st_mtime < _TTL_SECONDS:
            return cache.read_text() or None

    github = get_github_client()

    try:
        body = await github.get_file_content(repo, path)
    finally:
        await github.aclose()

    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(body or "")
    return body
