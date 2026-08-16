MAX_PAYLOAD_BYTES = 256 * 1024 * 1024
PAYLOAD_WARNING_BYTES = 200 * 1024 * 1024

BROWSER_SESSION_NAME_MAX_LENGTH = 64
BROWSER_SESSION_NAME_PATTERN = r"^[a-z](?:[a-z0-9-]*[a-z0-9])?$"
SITE_MAX_LENGTH = 255

# TTL-only, no renewal: must outlast the longest borrow, bounded by the
# browser container's 150-minute lease.
WRITER_LOCK_TTL_SECONDS = 3 * 60 * 60
SESSION_LAUNCH_TIMEOUT_SECONDS = 45
SESSION_EXPORT_TIMEOUT_SECONDS = 5 * 60

# A login window keeps only this Redis record; if the operator walks away it
# lapses and the container's own lease reaps the browser — nothing sweeps.
LOGIN_WINDOW_TTL_SECONDS = 30 * 60
LOGIN_WINDOW_KEY_PREFIX = "browser_login_window:"

VNC_PORT = 5900
SCREEN_CHUNK_BYTES = 64 * 1024
