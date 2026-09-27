from druks.chat.channels.base import Channel
from druks.chat.enums import ConversationSource

from .services import Voice


class CallsChannel(Channel):
    name = ConversationSource.CALL
    service = Voice
    has_turns = False
