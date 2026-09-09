from typing import TYPE_CHECKING

from .datastructures import Credentials
from .layout import get_remote_home

if TYPE_CHECKING:
    from .host import Host


async def push(host: "Host", credentials: Credentials) -> None:
    home = get_remote_home(host.ssh_username)
    for entry in credentials.home:
        await entry.push(host, home)
