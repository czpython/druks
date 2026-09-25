from druks.chat.channels.github.channel import GitHubChannel
from druks.core.services import Github
from druks.db import db_session
from druks.signals import subscribe


@subscribe("issue.commented", payload__author_can_write=True)
async def route_github_comment(*, repo: str, number: int, payload: dict, **_: object) -> None:
    await GitHubChannel.route_message(db_session(), await Github.get(), f"{repo}#{number}", payload)
