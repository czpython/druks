import re
import shlex

from .exceptions import ExecFailed
from .host import Host
from .layout import get_repo_root

# Reasonable cap for the clone-fetch-checkout chain. Most internal
# repos are well under a minute even with full history; a 10-minute
# cap absorbs slow Tailscale paths without letting a wedged git operation
# hang the run indefinitely.
CLONE_TIMEOUT_SECONDS = 600.0


async def clone(
    host: Host,
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
        target_path = get_repo_root(host.ssh_username)
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
    result = await host.exec(["sh", "-c", cmd], timeout=CLONE_TIMEOUT_SECONDS)
    if result.returncode:
        # Strip any token that somehow surfaced in error output before
        # surfacing — belt-and-braces. With the credential-helper the
        # token never lives in the URL, but git could still echo a
        # cached credential in some future error path.
        sanitized = _strip_token(result.stderr.strip() or result.stdout.strip())
        target = f"{repo_url}@{ref}" if ref else repo_url
        raise ExecFailed(
            f"git clone {target} failed: {sanitized}",
            exit_code=result.returncode,
        )


async def ensure(
    host: Host,
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
        target_path = get_repo_root(host.ssh_username)
    present = await host.exec(
        ["test", "-d", f"{target_path}/.git"],
        timeout=10.0,
    )
    if present.returncode:
        # Fresh VM (or first run on this PR) — full clone.
        await clone(host, repo_url=repo_url, ref=ref, target_path=target_path)
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
    result = await host.exec(["sh", "-c", cmd], timeout=CLONE_TIMEOUT_SECONDS)
    if result.returncode:
        sanitized = _strip_token(result.stderr.strip() or result.stdout.strip())
        raise ExecFailed(
            f"git fetch/checkout {repo_url}@{ref} failed: {sanitized}",
            exit_code=result.returncode,
        )


async def set_git_identity(
    host: Host,
    author: tuple[str, str],
    co_author: tuple[str, str] | None,
    *,
    repo_path: str,
) -> None:
    """Git authors each commit in the repo at ``repo_path`` as ``author``. A
    prepare-commit-msg hook credits ``co_author``. None credits nobody."""
    repo = shlex.quote(repo_path)
    await _write_git_identity(
        host, f"git -C {repo} config", f"{repo}/.git/hooks", author, co_author
    )


async def set_box_git_identity(
    host: Host, author: tuple[str, str], co_author: tuple[str, str] | None
) -> None:
    """Git authors each commit in the box as ``author``. Git copies a
    prepare-commit-msg hook that credits ``co_author`` into each repo that it
    clones or inits afterwards. None credits nobody."""
    template = '"$HOME"/.druks/git-template'
    # A template directory replaces git's default one, which holds info/exclude.
    setup = (
        f"mkdir -p {template}/hooks {template}/info",
        f"touch {template}/info/exclude",
        f"git config --global init.templateDir {template}",
    )
    await _write_git_identity(
        host, "git config --global", f"{template}/hooks", author, co_author, setup=setup
    )


async def _write_git_identity(
    host: Host,
    config: str,
    hooks: str,
    author: tuple[str, str],
    co_author: tuple[str, str] | None,
    *,
    setup: tuple[str, ...] = (),
) -> None:
    name, email = author
    hook_path = f"{hooks}/prepare-commit-msg"
    steps = [
        *setup,
        f"{config} user.name {shlex.quote(name)}",
        f"{config} user.email {shlex.quote(email)}",
    ]
    if co_author:
        trailer = f"Co-Authored-By: {co_author[0]} <{co_author[1]}>"
        # An empty message stays empty, so git still aborts the commit. The co-author's
        # line goes beside any other co-author line, and only once.
        hook = (
            "#!/bin/sh\n"
            'git stripspace --strip-comments < "$1" | grep -q . || exit 0\n'
            f"git interpret-trailers --in-place --if-exists addIfDifferent"
            f' --trailer {shlex.quote(trailer)} "$1"\n'
        )
        # A commit can run the hook while Druks rewrites it, so the new hook replaces
        # the old one in one rename.
        steps += [
            f"printf %s {shlex.quote(hook)} > {hook_path}.$$",
            f"chmod 755 {hook_path}.$$",
            f"mv -f {hook_path}.$$ {hook_path}",
        ]
    else:
        steps.append(f"rm -f {hook_path}")
    result = await host.exec(["sh", "-c", " && ".join(steps)], timeout=10.0)
    if result.returncode:
        raise ExecFailed(
            f"failed to set the git identity: "
            f"exit={result.returncode} stderr={result.stderr.strip()}",
            exit_code=result.returncode,
        )


def _strip_token(text: str) -> str:
    return re.sub(r"x-access-token:[^@]+@", "x-access-token:<redacted>@", text)
