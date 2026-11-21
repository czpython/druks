"""One classifier for the Bash commands the measurement scripts time.

ci-survey.py, timeline.py, and analyse.py all import this, so a command lands in
the same group everywhere. A command is "waiting" when it sleeps or polls CI,
"full suite" when it runs the whole backend test suite, and "work" otherwise.
The 0-minute target is the sum of the waiting and the full-suite minutes.
"""

WAITING = ("sleep ", "gh pr checks", "gh run watch", "gh run list", "gh run rerun")
SUITE_TARGETS = ("backend/", "backend/tests", "backend/tests/")


def classify(command):
    """"waiting", "full suite", or "work" for one Bash command."""
    text = command + " "
    if any(word in text for word in WAITING):
        return "waiting"
    # `gh run view` watches a run, unless it reads the logs with --log/--log-failed.
    if "gh run view" in text and "--log" not in text:
        return "waiting"
    if "pytest" in text and _is_full_suite(text):
        return "full suite"
    return "work"


def _is_full_suite(text):
    """The whole backend suite: the directory backend/ or backend/tests itself, or
    --locked at the repo root. A file path, a ::test id, or -k selects less, so work."""
    tokens = text.split()
    if any(token.endswith(".py") for token in tokens):
        return False
    if " -k " in f" {text} " or "::" in text:
        return False
    if "--locked" in text:
        return True
    return any(token in SUITE_TARGETS for token in tokens)
