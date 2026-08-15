MAX_PAYLOAD_BYTES = 256 * 1024 * 1024
PAYLOAD_WARNING_BYTES = 200 * 1024 * 1024

BROWSER_SESSION_NAME_MAX_LENGTH = 64
BROWSER_SESSION_NAME_PATTERN = r"^[a-z](?:[a-z0-9-]*[a-z0-9])?$"
SITE_MAX_LENGTH = 255

WRITER_LOCK_TTL_SECONDS = 60
SESSION_LAUNCH_TIMEOUT_SECONDS = 45
SESSION_EXPORT_TIMEOUT_SECONDS = 5 * 60

# A login window holds the session's writer lock and its Redis record for as
# long as it may stay open; both lapse together if the operator walks away, and
# the container's own lease reaps it — nothing sweeps.
LOGIN_WINDOW_TTL_SECONDS = 30 * 60
LOGIN_WINDOW_KEY_PREFIX = "browser_login_window:"

VNC_PORT = 5900
SCREEN_CHUNK_BYTES = 64 * 1024
