from druks.chat.channels.github.channel import GitHubChannel
from druks.core.services import Github
from druks.db import db_session
from druks.signals import subscribe


@subscribe(
    "ticket.commented",
    payload__source="github",
    payload__author_is_bot=False,
    payload__author_can_write=True,
)
async def route_issue_comment(*, payload: dict, **_: object) -> None:
    await GitHubChannel.route_message(
        db_session(), await Github.get(), payload["identifier"], payload
    )


@subscribe("pr.commented", payload__author_is_bot=False, payload__author_can_write=True)
async def route_pull_request_comment(
    *, repo: str, pr_number: int, payload: dict, **_: object
) -> None:
    await GitHubChannel.route_message(
        db_session(),
        await Github.get(),
        f"{repo}#{pr_number}",
        payload,
        review_thread_id=payload["review_thread_id"],
    )
