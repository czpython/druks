from druks.secrets.datastructures import Audience

WAHA_AUDIENCE = Audience.service("waha")
# These engines name a message by its chat. WEBJS names a message that the phone sent
# by the account's own number, so Druks cannot find its chat.
SUPPORTED_ENGINES = ("NOWEB", "GOWS")
