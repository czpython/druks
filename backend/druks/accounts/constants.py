import string
from datetime import timedelta

# Owns every run nobody asked for: crons, background work. Seeded at first
# start; no provider email ever collides with it.
SYSTEM_ACCOUNT_ID = "system"

# A personal access token serializes as druks_pat_<prefix>_<secret>. The prefix
# is the row's lookup key and the only part that may appear in errors, logs, or
# lists; the secret exists only inside the copy-once plaintext.
PAT_TOKEN_TAG = "druks_pat"
PAT_NAME_LENGTH = 80
PAT_PREFIX_LENGTH = 12
# No separator characters, so the serialized token splits unambiguously on "_".
PAT_PREFIX_ALPHABET = string.ascii_letters + string.digits
PAT_SECRET_BYTES = 32  # 43 base64url characters
PAT_LIFETIME = timedelta(days=365)
# last_used_at advances at most this often — a busy token must not write a row
# per request.
PAT_LAST_USED_RESOLUTION = timedelta(hours=1)
