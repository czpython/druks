import re
import shlex

from .exceptions import ExecFailed
from .host import Sandbox
from .layout import get_repo_root

# Reasonable cap for the clone-fetch-checkout chain. Most internal
# repos are well under a minute even with full history; a 10-minute
# cap absorbs slow Tailscale paths without letting a wedged git operation
# hang the run indefinitely.
CLONE_TIMEOUT_SECONDS = 600.0


async def clone(
    sandbox: Sandbox,
    *,
    repo_url: str,
    ref: str | None,
    target_path: str | None = None,
) -> None:
    if not repo_url.startswith("https://github.com/"):
        raise ValueError(
            f"repo_url must start with https://github.com/, got {repo_url!r}",
        )

    if not target_path:
        target_path = get_repo_root(sandbox.ssh_username)
    quoted_url = shlex.quote(repo_url)
    quoted_target = shlex.quote(target_path)
    parent = target_path.rsplit("/", 1)[0]
    quoted_parent = shlex.quote(parent) if parent else "."

    # Plain clone — git consults the credential helper for github.com
    # auth. The chain:
    #   1. mkdir -p the target's parent
    #   2. clone the plain URL
    #   3. (if ref given) cd in, fetch the ref, check out a LOCAL branch
    #      named after it. A named branch plus ``push.default current``
    #      means a plain ``git push`` lands on the right remote branch
    #      (a detached HEAD would need an explicit refspec).
    cmd = f"mkdir -p {quoted_parent} && git clone {quoted_url} {quoted_target}"
    if ref:
        quoted_ref = shlex.quote(ref)
        cmd += (
            f" && cd {quoted_target} && git fetch origin {quoted_ref}"
            f" && git checkout -B {quoted_ref} FETCH_HEAD"
            f" && git config push.default current"
        )
    result = await sandbox.exec(["sh", "-c", cmd], timeout=CLONE_TIMEOUT_SECONDS)
    if not result.ok:
        # Strip any token that somehow surfaced in error output before
        # surfacing — belt-and-braces. With the credential-helper the
        # token never lives in the URL, but git could still echo a
        # cached credential in some future error path.
        sanitized = _strip_token(result.stderr.strip() or result.stdout.strip())
        target = f"{repo_url}@{ref}" if ref else repo_url
        raise ExecFailed(
            f"git clone {target} failed: {sanitized}",
            exit_code=result.exit_code,
        )


async def ensure(
    sandbox: Sandbox,
    *,
    repo_url: str,
    ref: str | None,
    target_path: str | None = None,
) -> None:
    if not repo_url.startswith("https://github.com/"):
        raise ValueError(
            f"repo_url must start with https://github.com/, got {repo_url!r}",
        )

    if not target_path:
        target_path = get_repo_root(sandbox.ssh_username)
    present = await sandbox.exec(
        ["test", "-d", f"{target_path}/.git"],
        timeout=10.0,
    )
    if not present.ok:
        # Fresh VM (or first run on this PR) — full clone.
        await clone(sandbox, repo_url=repo_url, ref=ref, target_path=target_path)
        return

    if not ref:
        # Reference repo already present; any commit is fine.
        return

    quoted_target = shlex.quote(target_path)
    quoted_ref = shlex.quote(ref)
    # Same named-branch checkout as ``clone`` (see comment there); ``-fB``
    # both discards local drift on a warm VM and (re)points the branch.
    cmd = (
        f"cd {quoted_target} && git fetch origin {quoted_ref}"
        f" && git checkout -fB {quoted_ref} FETCH_HEAD"
        f" && git config push.default current"
    )
    result = await sandbox.exec(["sh", "-c", cmd], timeout=CLONE_TIMEOUT_SECONDS)
    if not result.ok:
        sanitized = _strip_token(result.stderr.strip() or result.stdout.strip())
        raise ExecFailed(
            f"git fetch/checkout {repo_url}@{ref} failed: {sanitized}",
            exit_code=result.exit_code,
        )


def _strip_token(text: str) -> str:
    return re.sub(r"x-access-token:[^@]+@", "x-access-token:<redacted>@", text)
