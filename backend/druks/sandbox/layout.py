# The in-VM filesystem layout: where the helper script and workspace live on a
# sandbox host, all derived from the SSH user's home so
# non-root users (e.g. ``exedev`` on the standard exuntu image) can ``mkdir``
# and own every subdir.


def get_remote_home(ssh_username: str) -> str:
    return "/root" if ssh_username == "root" else f"/home/{ssh_username}"


def get_helper_script_path(ssh_username: str) -> str:
    # Where the uploaded ``druks-sandbox`` helper script lands.
    return f"{get_remote_home(ssh_username)}/druks-sandbox"


def get_work_root(ssh_username: str) -> str:
    return f"{get_remote_home(ssh_username)}/work"


def get_repo_root(ssh_username: str) -> str:
    return f"{get_work_root(ssh_username)}/repo"


def get_runs_root(ssh_username: str) -> str:
    return f"{get_work_root(ssh_username)}/runs"


def get_related_root(ssh_username: str) -> str:
    return f"{get_work_root(ssh_username)}/related"


def get_github_token_remote_path(ssh_username: str) -> str:
    return f"{get_work_root(ssh_username)}/github-token"
