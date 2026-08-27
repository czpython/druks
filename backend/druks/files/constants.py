from datetime import timedelta

MAX_FILE_BYTES = 10 * 1024 * 1024 * 1024  # 10 GiB
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
