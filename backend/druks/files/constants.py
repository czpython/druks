from datetime import timedelta

MAX_FILE_BYTES = 10 * 1024 * 1024 * 1024  # 10 GiB
# A person picking a file in a browser, not an agent shipping a workspace.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MiB
REAPER_GRACE_PERIOD = timedelta(days=1)

INLINE_CONTENT_TYPES = {
    "application/pdf",
    "image/avif",
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/webp",
    "text/plain",
}
