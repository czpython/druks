from enum import StrEnum


class AccountKind(StrEnum):
    """Who holds the account. Only an operator signs in; a bot and a bot admin
    account act for a channel connection of an app's Bot, such as a linked number."""

    OPERATOR = "operator"
    BOT = "bot"
    BOT_ADMIN = "bot_admin"
