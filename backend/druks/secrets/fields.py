import json
from collections.abc import Iterator, MutableMapping
from typing import Any

from sqlalchemy import LargeBinary
from sqlalchemy.ext.mutable import Mutable
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from druks.secrets import utils


class _EncryptedColumn(TypeDecorator):
    # Bytes in the DB. The owning ``table.column``, captured when the column
    # attaches to its table, is the envelope AAD.
    impl = LargeBinary
    cache_ok = True

    _aad = ""

    def _set_parent(self, parent: Any, **kw: Any) -> None:
        super()._set_parent(parent, **kw)
        parent._on_table_attach(self._set_aad)

    def _set_aad(self, column: Any, table: Any) -> None:
        self._aad = f"{table.name}.{column.name}"


class Secret:
    # The live value of an EncryptedTextField column: plaintext exists only
    # where decrypt() is called — loading a row never touches key material,
    # and truthiness reads the stored bytes alone, so presence checks don't
    # either. Redacted in repr so a logged row can't leak.

    def __init__(self, envelope: bytes, aad: str) -> None:
        self._envelope = envelope
        self._aad = aad

    def decrypt(self) -> str:
        if not self._envelope:
            return ""
        return utils.decrypt(self._envelope, self._aad).decode()

    def __bool__(self) -> bool:
        return bool(self._envelope)

    def __repr__(self) -> str:
        return "Secret(<redacted>)"

    __str__ = __repr__


class EncryptedText(_EncryptedColumn):
    def process_bind_param(self, value: Any, dialect: Any) -> bytes:
        # Assignment takes a plain str; a Secret carried over from another
        # column re-encrypts under this column's AAD via its plaintext. An
        # empty value stores as empty bytes — no envelope.
        if isinstance(value, Secret):
            value = value.decrypt()
        if not isinstance(value, str):
            raise ValueError(f"an encrypted text column takes a str, not {type(value).__name__}")
        if not value:
            return b""
        return utils.encrypt(value.encode(), self._aad)

    def process_result_value(self, value: Any, dialect: Any) -> Secret:
        return Secret(bytes(value), self._aad)


class SecretBytes:
    # The live value of an EncryptedBytesField column: plaintext exists only
    # where decrypt() is called, presence checks read the stored bytes alone,
    # and repr can't leak. The bytes twin of Secret.

    def __init__(self, envelope: bytes, aad: str) -> None:
        self._envelope = envelope
        self._aad = aad

    def decrypt(self) -> bytes:
        if not self._envelope:
            return b""
        return utils.decrypt(self._envelope, self._aad)

    def __bool__(self) -> bool:
        return bool(self._envelope)

    def __repr__(self) -> str:
        return "SecretBytes(<redacted>)"

    __str__ = __repr__


class EncryptedBytes(_EncryptedColumn):
    def process_bind_param(self, value: Any, dialect: Any) -> bytes:
        # Assignment takes plain bytes; a SecretBytes carried over from
        # another column re-encrypts under this column's AAD via its
        # plaintext. An empty value stores as empty bytes — no envelope.
        if isinstance(value, SecretBytes):
            value = value.decrypt()
        if not isinstance(value, bytes):
            raise ValueError(f"an encrypted bytes column takes bytes, not {type(value).__name__}")
        if not value:
            return b""
        return utils.encrypt(value, self._aad)

    def process_result_value(self, value: Any, dialect: Any) -> SecretBytes:
        return SecretBytes(bytes(value), self._aad)


class SecretsMapping(Mutable, MutableMapping):
    # The live value of an EncryptedJsonField column: dict-shaped, decrypting
    # lazily on first read and redacted in repr. In-place writes mark the
    # column dirty through Mutable — `row.secrets["token"] = ...` persists on
    # its own merit at flush.

    def __init__(self, data: dict | None = None, *, envelope: bytes = b"", aad: str = "") -> None:
        self._data: dict | None = None if envelope else dict(data or {})
        self._envelope = envelope
        self._aad = aad

    @classmethod
    def coerce(cls, key: str, value: Any) -> "SecretsMapping":
        # Attribute assignment takes a plain dict; anything else is a bug,
        # rejected loudly.
        if isinstance(value, cls):
            return value
        if isinstance(value, dict):
            return cls(value)
        raise ValueError(f"{key} takes a dict, not {type(value).__name__}")

    def _decrypted(self) -> dict:
        if self._data is None:
            self._data = json.loads(utils.decrypt(self._envelope, self._aad))
            self._envelope = b""
        return self._data

    def __getitem__(self, key: str) -> Any:
        return self._decrypted()[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self._decrypted()[key] = value
        self.changed()

    def __delitem__(self, key: str) -> None:
        del self._decrypted()[key]
        self.changed()

    def __iter__(self) -> Iterator[str]:
        return iter(self._decrypted())

    def __len__(self) -> int:
        return len(self._decrypted())

    def __repr__(self) -> str:
        return "SecretsMapping(<redacted>)"

    __str__ = __repr__


class EncryptedJson(_EncryptedColumn):
    def process_bind_param(self, value: Any, dialect: Any) -> bytes:
        # A plain dict arrives from mapped_column's default=dict; everything
        # else is a SecretsMapping. dict() re-reads the mapping, so a value
        # carried over from another column re-encrypts under this one's AAD.
        plaintext = json.dumps(dict(value), separators=(",", ":"), sort_keys=True)
        return utils.encrypt(plaintext.encode(), self._aad)

    def process_result_value(self, value: Any, dialect: Any) -> SecretsMapping:
        return SecretsMapping(envelope=bytes(value), aad=self._aad)


def EncryptedTextField(**kwargs: Any) -> Mapped[Secret]:
    # Declared on a model as ``token = EncryptedTextField(default="")``: a
    # NOT NULL column taking a str in, handing a Secret back, ciphertext at
    # rest. Reassignment is the write path — there is no in-place mutation.
    # Without an explicit nullable, SQLAlchemy has no ``Mapped[...]``
    # annotation to infer from and would default the column to NULL.
    kwargs.setdefault("nullable", False)
    return mapped_column(EncryptedText(), **kwargs)


def EncryptedBytesField(**kwargs: Any) -> Mapped[SecretBytes]:
    # Declared on a model as ``payload = EncryptedBytesField(default=b"")``:
    # a NOT NULL column taking bytes in, handing a SecretBytes back,
    # ciphertext at rest. Reassignment is the write path.
    kwargs.setdefault("nullable", False)
    return mapped_column(EncryptedBytes(), **kwargs)


def EncryptedJsonField() -> Mapped[SecretsMapping]:
    # Declared on a model as ``secrets = EncryptedJsonField()``: a NOT NULL
    # dict column, ciphertext at rest, for secrets that are genuinely a
    # mapping. A single secret belongs in an EncryptedTextField column.
    return mapped_column(SecretsMapping.as_mutable(EncryptedJson()), default=dict, nullable=False)
