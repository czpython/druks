from typing import TYPE_CHECKING

from .datastructures import Credentials
from .layout import (
    get_claude_credentials_remote,
    get_codex_auth_remote,
    get_github_token_remote_path,
    get_remote_home,
)

if TYPE_CHECKING:
    from .host import Host

# Tar excludes for credential-dir uploads. These never carry value into the
# VM and dominate upload time when shipped naively:
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


async def push(host: "Host", creds: Credentials) -> None:
    if creds.claude_credentials:
        await host.write_secret(
            secret=creds.claude_credentials,
            remote=get_claude_credentials_remote(host.ssh_username),
        )
    if creds.codex_credentials:
        await host.write_secret(
            secret=creds.codex_credentials,
            remote=get_codex_auth_remote(host.ssh_username),
        )
    if creds.github_token is not None:
        # TODO: This token expires in ~60 min and is not refreshed, so a run
        # outliving it 401s on late git pushes. The in-VM credential helper
        # should mint on demand from a druks token-broker endpoint, retiring
        # this static file.
        await host.write_secret(
            secret=creds.github_token,
            remote=get_github_token_remote_path(host.ssh_username),
        )

    home = get_remote_home(host.ssh_username)
    for local, dest_suffix in creds.extra_config_files:
        # Optional — a dev box may not have config.toml etc., and a Docker bind
        # mount whose host file never existed leaves a directory at the path.
        # Push real files only; missing config isn't fatal (the CLIs mint their
        # own defaults in the VM).
        if not local.is_file():
            continue
        await host.upload_file(local=local, remote=f"{home}/{dest_suffix}")
    for local, dest_suffix in creds.extra_config_dirs:
        if not local.is_dir():
            continue
        await host.upload_dir(
            local=local,
            remote=f"{home}/{dest_suffix}",
            excludes=DEFAULT_DIR_EXCLUDES + creds.extra_dir_excludes.get(dest_suffix, ()),
        )
