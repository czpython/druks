from druks.chat.channels.slack.channel import SlackChannel
from druks.core.services import Slack
from druks.db import db_session
from druks.signals import subscribe


@subscribe("slack.message")
async def route_slack_message(*, message: dict, **_: object) -> None:
    await SlackChannel.route_message(db_session(), await Slack.get(), message)
