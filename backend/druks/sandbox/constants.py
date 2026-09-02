# A host leases for a fixed span; drukbox reaps it when the lease lapses, so a
# run whose worker dies frees its VM with no druks-side sweep. The lease must
# outlast any single continuous hold: agent calls are capped at
# MAX_AGENT_TIMEOUT_SECONDS, and a warm host rotates before its remaining lease
# can no longer cover one more worst-case call.
MAX_AGENT_TIMEOUT_SECONDS = 65 * 60  # 65 min — the existing sandbox-run horizon
SANDBOX_HOST_LEASE_SECONDS = 150 * 60  # 150 min — fits two back-to-back worst-case calls
SANDBOX_HOST_ROTATE_BEFORE_SECONDS = MAX_AGENT_TIMEOUT_SECONDS + 10 * 60  # 75 min (65 + 10 margin)

# The per-connection rotation gate in Redis: the flag that shuts the gate while
# a refresh runs, and the zset of active users scored by expiry.
ROTATING_PREFIX = "druks:sandbox:rotating:"
GATE_USERS_PREFIX = "druks:sandbox:gate:users:"

# Tar excludes for every tree copied into a sandbox home. These never carry
# value into the VM and dominate upload time when shipped naively:
#
# - ``.in_use`` — Claude's plugin cache writes a marker file per *host* PID
#   to track which processes pin a plugin version. The VM has a fresh PID
#   space, so our host's markers are meaningless noise — and there are
#   thousands of them across the plugin tree.
# - ``.git`` — marketplace plugin checkouts ship the full repo metadata.
# - ``node_modules`` — some plugins (e.g. ones with TS tooling) ship deps
#   that re-install fine inside the VM.
# - ``__pycache__`` / ``*.pyc`` — Python bytecode is host-arch sensitive.
DEFAULT_DIR_EXCLUDES: tuple[str, ...] = (
    ".in_use",
    ".git",
    "node_modules",
    "__pycache__",
    "*.pyc",
)
