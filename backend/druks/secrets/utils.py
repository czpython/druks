import base64
import os
from functools import lru_cache

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from druks.secrets.exceptions import SecretDecryptError
from druks.settings import load_settings

# The stored envelope, byte-exact:
#
#   version[1] || salt[16] || nonce[12] || ciphertext[n] || tag[16]
#
# The salt derives this envelope's AES key (HKDF over a configured master key —
# the master never touches a cipher directly, and a fresh key per envelope
# makes nonce reuse a non-concern); AES-GCM returns ciphertext with the tag
# appended. The AAD names the owning scope — ``table.column`` for encrypted
# columns, ``browser_session:<id>:<version>`` for vault envelopes — so a blob
# can't be replayed anywhere else.
_ENVELOPE_V1 = b"\x01"
_SALT_LEN = 16
_NONCE_LEN = 12
_HEADER_LEN = 1 + _SALT_LEN + _NONCE_LEN
_HKDF_INFO = b"druks-secrets-v1"


@lru_cache
def keys(raw: str) -> tuple[bytes, ...]:
    # ``raw`` arrives settings-validated (Settings.secrets.secrets_key): non-empty,
    # every segment base64 for 32 bytes. First key encrypts, every key
    # decrypts.
    return tuple(base64.b64decode(segment.strip()) for segment in raw.split(","))


def _derive_key(master: bytes, salt: bytes) -> bytes:
    return HKDF(algorithm=SHA256(), length=32, salt=salt, info=_HKDF_INFO).derive(master)


def encrypt(plaintext: bytes, aad: str) -> bytes:
    salt = os.urandom(_SALT_LEN)
    nonce = os.urandom(_NONCE_LEN)
    ciphertext = AESGCM(_derive_key(keys(load_settings().secrets.secrets_key)[0], salt)).encrypt(
        nonce, plaintext, aad.encode()
    )
    return _ENVELOPE_V1 + salt + nonce + ciphertext


def decrypt(envelope: bytes, aad: str) -> bytes:
    # No structural checks: the GCM tag authenticates ciphertext + AAD, so a
    # truncated, garbled, or foreign envelope fails the same way a wrong key
    # does (a mangled nonce raises ValueError instead of InvalidTag) — every
    # unreadable shape is the one named error.
    salt = envelope[1 : 1 + _SALT_LEN]
    nonce = envelope[1 + _SALT_LEN : _HEADER_LEN]
    ciphertext = envelope[_HEADER_LEN:]
    for master in keys(load_settings().secrets.secrets_key):
        try:
            return AESGCM(_derive_key(master, salt)).decrypt(nonce, ciphertext, aad.encode())
        except (InvalidTag, ValueError):
            continue
    raise SecretDecryptError()
