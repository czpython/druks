from typing import TYPE_CHECKING

from .datastructures import Credentials
from .layout import get_github_token_remote_path, get_remote_home

if TYPE_CHECKING:
    from .host import Host


async def push(host: "Host", credentials: Credentials) -> None:
    home = get_remote_home(host.ssh_username)
    for entry in credentials.home:
        await entry.push(host, home)
    if credentials.github_token:
        # TODO: This token expires in ~60 min and is not refreshed, so a run
        # outliving it 401s on late git pushes. The in-VM credential helper
        # should mint on demand from a druks token-broker endpoint, retiring
        # this static file.
        await host.write_secret(
            secret=credentials.github_token,
            remote=get_github_token_remote_path(host.ssh_username),
        )
