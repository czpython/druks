# Redis keys for harness connections: a pending connect flow's state while the
# operator completes it, and the per-connection SET NX lock that serializes
# refresh.
CONNECT_PENDING_PREFIX = "druks:harness:connect:pending:"
REFRESH_LOCK_PREFIX = "druks:harness:refresh:"
