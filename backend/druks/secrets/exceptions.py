class SecretDecryptError(Exception):
    # A stored secret exists but none of the configured keys decrypt it — the
    # usual cause is a key dropped from secrets.secrets_key while rows still
    # encrypted under it existed. Named so a delivery failure points at the
    # key config, not a bare crypto traceback.
    def __init__(self) -> None:
        super().__init__(
            "A stored secret could not be decrypted with the configured "
            "secrets.secrets_key keys. If a key was rotated out, prepend the "
            "current key with it again; otherwise re-enter the secret."
        )
