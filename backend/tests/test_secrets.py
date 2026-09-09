import base64
import os

import pytest
from druks.mcp.constants import BEARER_HEADER
from druks.mcp.models import McpServer
from druks.secrets.datastructures import Audience
from druks.secrets.enums import SecretKind
from druks.secrets.models import VaultSecret
from druks.settings import load_settings
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy_encrypted_field import SecretDecryptError

_TOKEN = "lin_secret_value"


def _key() -> str:
    return base64.b64encode(os.urandom(32)).decode()


def _set_key(monkeypatch, tmp_path, value: str) -> None:
    config_path = tmp_path / "druks.toml"
    config_path.write_text(f'[secrets]\nsecrets_key = "{value}"\n')
    monkeypatch.setenv("DRUKS_CONFIG", str(config_path))


async def _store_token(token: str = _TOKEN) -> None:
    # The paste path: a server's bearer lands in the vault under its header.
    await McpServer.create(name="linear", url="https://mcp.linear.app/sse", token=token)


async def _get_token() -> VaultSecret:
    return await VaultSecret.lookup(SecretKind.STATIC, Audience.mcp("linear"), header=BEARER_HEADER)


async def _store_grant(refresh_token: str = "rt-secret", client_secret: str = "") -> VaultSecret:
    await McpServer.get_for_name("notion") or await McpServer.create(
        name="notion", url="https://mcp.notion.test/sse"
    )
    return await VaultSecret.connect(
        Audience.mcp("notion"),
        account_id=None,
        refresh_token=refresh_token,
        scopes=[],
        secrets={
            "token_endpoint": "https://auth.test/token",
            "client_id": "client-123",
            "client_secret": client_secret,
        },
    )


async def test_stored_secrets_are_ciphertext_and_reads_restore_them(druks_db):
    await _store_token()

    blob = bytes((await druks_db.execute(text("SELECT secrets FROM vault"))).scalar_one())
    assert _TOKEN.encode() not in blob
    druks_db.expunge_all()
    assert (await _get_token()).secrets["value"] == _TOKEN
    # The merged view every consumer reads carries the vault row itself, so
    # the plaintext exists only where the value is read.
    merged = (await McpServer._merged())["linear"]
    assert merged["token"].secrets["value"] == _TOKEN


async def test_grant_secret_halves_round_trip(druks_db):
    await _store_grant(refresh_token="rt-secret", client_secret="cs-secret")

    druks_db.expunge_all()
    [grant] = await VaultSecret.list_connections(Audience.mcp("notion"))
    assert grant.secrets["refresh_token"] == "rt-secret"
    assert grant.secrets["client_secret"] == "cs-secret"


async def test_loaded_secrets_are_lazy_and_redacted(monkeypatch, tmp_path, druks_db):
    await _store_token()
    druks_db.expunge_all()

    # Loading and logging a row never touches key material — decryption
    # happens only on a read of a value, and repr leaks nothing either way.
    row = await _get_token()
    _set_key(monkeypatch, tmp_path, "")
    assert repr(row.secrets) == "SecretsMapping(<redacted>)"
    with pytest.raises(ValidationError, match="Field required"):
        row.secrets["value"]


def test_missing_key_refuses_boot(monkeypatch, tmp_path):
    # Blank and comma-noise-only both read as "no key" — the required setting
    # refuses at construction rather than falling back to plaintext.
    for broken in ("", ",", " , "):
        _set_key(monkeypatch, tmp_path, broken)
        with pytest.raises(ValidationError, match="Field required|at least one"):
            load_settings()


def test_key_validation_error_never_echoes_the_key(monkeypatch, tmp_path):
    # A half-valid list fails validation, and the failure surfaces in boot
    # logs and doctor output — it must not echo the valid segment.
    good = _key()
    _set_key(monkeypatch, tmp_path, f"{good},not-base64!!")

    with pytest.raises(ValidationError) as error_info:
        load_settings()
    assert good not in str(error_info.value)


def test_malformed_key_refuses_boot(monkeypatch, tmp_path):
    for broken in ("not-base64!!", base64.b64encode(b"short").decode()):
        _set_key(monkeypatch, tmp_path, broken)
        with pytest.raises(ValidationError, match="base64|32 bytes"):
            load_settings()


async def test_undecryptable_secret_raises_the_named_error(monkeypatch, tmp_path, druks_db):
    # A key dropped from the list while rows written under it existed is the
    # usual cause — the error must say so, not surface a bare crypto traceback.
    await _store_token()
    druks_db.expunge_all()
    _set_key(monkeypatch, tmp_path, _key())

    with pytest.raises(SecretDecryptError, match="rotated out"):
        (await _get_token()).secrets["value"]


async def test_garbled_envelope_raises_the_named_error(druks_db):
    # No structural pre-checks in decrypt: GCM authentication (and the
    # ValueError a mangled nonce raises) fold every unreadable shape into the
    # one named error.
    await _store_token()
    await druks_db.execute(text(r"UPDATE vault SET secrets = '\x01ab'::bytea"))
    druks_db.expunge_all()

    with pytest.raises(SecretDecryptError):
        (await _get_token()).secrets["value"]


async def test_prepended_key_still_decrypts(monkeypatch, tmp_path, druks_db):
    # Rotation is prepend-only: new writes use the first key; rows written
    # under an older key keep decrypting as long as it stays in the list.
    old_key = _key()
    _set_key(monkeypatch, tmp_path, old_key)
    await _store_token()
    await _store_grant(refresh_token="rt-secret")

    _set_key(monkeypatch, tmp_path, f"{_key()},{old_key}")
    druks_db.expunge_all()
    assert (await _get_token()).secrets["value"] == _TOKEN
    [grant] = await VaultSecret.list_connections(Audience.mcp("notion"))
    assert grant.secrets["refresh_token"] == "rt-secret"
