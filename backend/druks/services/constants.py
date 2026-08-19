# OAuth connect + mint plumbing shared by every OauthClient consumer. Pending
# connect state (the PKCE verifier plus the begun flow's client identity)
# lives in Redis for its short TTL, single-use. Access tokens cache for their
# lifetime minus the skew, so a token handed out never expires moments after
# delivery.
OAUTH_CONNECT_STATE_TTL_SECONDS = 600
OAUTH_TOKEN_TTL_SKEW_SECONDS = 60

# Mint's mutual exclusion, in the Redis that fronts the token cache (SET NX):
# a rotating grant tolerates exactly one refresher. The lock TTL is a crash
# backstop at three times the HTTP client's timeout — a live refresh cannot
# outlive it. Losers poll the cache on the interval for about one
# token-endpoint round trip, then fail loudly.
OAUTH_REFRESH_LOCK_TTL_SECONDS = 90
OAUTH_MINT_WAIT_INTERVAL_SECONDS = 0.2
OAUTH_MINT_WAIT_ATTEMPTS = 150
