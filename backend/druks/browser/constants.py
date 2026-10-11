MAX_PAYLOAD_BYTES = 256 * 1024 * 1024
PAYLOAD_WARNING_BYTES = 200 * 1024 * 1024

BROWSER_SESSION_NAME_MAX_LENGTH = 64
SITE_MAX_LENGTH = 255

SESSION_LAUNCH_TIMEOUT_SECONDS = 45
SESSION_EXPORT_TIMEOUT_SECONDS = 5 * 60
CDP_CONNECT_TIMEOUT_SECONDS = 30
# PinchTab gives a page 60 seconds to load; a request waits longer than that.
PINCHTAB_REQUEST_TIMEOUT_SECONDS = 90
PINCHTAB_PORT = 9867
# An agent call's browser keeps only this Redis record, under the call's key:
# the box, the bridge's token, and the tab. The tools refuse the key once the
# record is gone.
AGENT_BROWSER_KEY_PREFIX = "agent_browser:"
# The tools an agent call's key allows: the browser routes' operation ids.
BROWSER_TOOLS = (
    "browser_navigate",
    "browser_evaluate",
    "browser_snapshot",
    "browser_text",
    "browser_action",
)

# A login window keeps only this Redis record; if the operator walks away it
# lapses and the container's own lease reaps the browser — nothing sweeps.
LOGIN_WINDOW_TTL_SECONDS = 30 * 60
LOGIN_WINDOW_KEY_PREFIX = "browser_login_window:"

VNC_PORT = 5900
SCREEN_CHUNK_BYTES = 64 * 1024

# The fatal a bounced borrow announces; a subscriber marks the named session stale.
SESSION_SIGNED_OUT_SIGNAL = "browser.session_signed_out"
