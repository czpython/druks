import uuid

from druks.browser.constants import WRITER_LOCK_TTL_SECONDS
from druks.browser.exceptions import BrowserSessionWriterLockedError
from druks.redis import get_client

# Check the token is still ours before deleting, atomically — a plain
# get-then-del could delete a successor's lock in the gap between the two.
_RELEASE = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


def writer_lock_key(session_id: str) -> str:
    return f"browser_session:{session_id}"


async def acquire_writer_lock(session_id: str) -> str:
    """Hold the session against another persisting writer. Returns the owner
    token to release with. The TTL frees the lock if the holder's process
    dies; it outlasts any real borrow, so the lock never expires under a live
    one."""
    token = uuid.uuid4().hex
    held = await get_client().set(
        writer_lock_key(session_id),
        token,
        nx=True,
        ex=WRITER_LOCK_TTL_SECONDS,
    )
    if held:
        return token
    raise BrowserSessionWriterLockedError(session_id)


async def release_writer_lock(session_id: str, token: str) -> None:
    await get_client().eval(_RELEASE, 1, writer_lock_key(session_id), token)
