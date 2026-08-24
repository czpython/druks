import base64
import os

import pytest
from druks.accounts.constants import SYSTEM_ACCOUNT_ID
from druks.core.models import Uuid7Pk
from druks.database import db_session
from druks.mcp.models import McpClientRegistration, McpServer
from druks.models import Base
from druks.secrets.exceptions import SecretDecryptError
from druks.secrets.fields import EncryptedJsonField
from druks.services.models import OauthConnection
from druks.settings import load_settings
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.exc import StatementError

_TOKEN = "lin_secret_value"


class EncryptedNote(Base, Uuid7Pk):
    # Test-only consumer of EncryptedJsonField: the MCP columns are all single
    # values (EncryptedTextField); the mapping field ships for secrets that
    # are genuinely a mapping.
    __tablename__ = "test_encrypted_notes"

    data = EncryptedJsonField()


def _key() -> str:
    return base64.b64encode(os.urandom(32)).decode()


def _set_key(monkeypatch, tmp_path, value: str) -> None:
    config_path = tmp_path / "druks.toml"
    config_path.write_text(f'[secrets]\nsecrets_key = "{value}"\n')
    monkeypatch.setenv("DRUKS_CONFIG", str(config_path))


async def _store_grant(
    refresh_token: str = "rt-secret", client_secret: str = ""
) -> OauthConnection:
    server = await McpServer.get_for_name("notion") or await McpServer.create(
        name="notion", url="https://mcp.notion.test/sse"
    )
    await McpClientRegistration.store(
        server_id=server.id,
        account_id=SYSTEM_ACCOUNT_ID,
        token_endpoint="https://auth.test/token",
        client_id="client-123",
        client_secret=client_secret,
    )
    return await OauthConnection.create(
        provider="mcp:notion",
        account_id=SYSTEM_ACCOUNT_ID,
        refresh_token=refresh_token,
        scopes=[],
    )


async def test_stored_secrets_are_ciphertext_and_reads_restore_them(druks_db):
    await McpServer.create(name="linear", url="https://mcp.linear.app/sse", token=_TOKEN)

    blob = bytes((await druks_db.execute(text("SELECT token FROM mcp_servers"))).scalar_one())
    assert _TOKEN.encode() not in blob
    druks_db.expunge_all()
    row = await McpServer.get_for_name("linear")
    assert row.token.decrypt() == _TOKEN
    # The merged view every consumer reads carries the Secret itself, so the
    # plaintext exists only where decrypt() is called.
    merged = (await McpServer._merged())["linear"]
    assert merged["token"].decrypt() == _TOKEN


async def test_grant_secret_halves_round_trip(druks_db):
    await _store_grant(refresh_token="rt-secret", client_secret="cs-secret")

    druks_db.expunge_all()
    grant = (await OauthConnection.list_for_account("mcp:notion", SYSTEM_ACCOUNT_ID))[0]
    registration = await McpClientRegistration.get_for_account("notion", SYSTEM_ACCOUNT_ID)
    assert grant.refresh_token.decrypt() == "rt-secret"
    assert registration.client_secret.decrypt() == "cs-secret"


async def test_loaded_secrets_are_lazy_and_redacted(monkeypatch, tmp_path, druks_db):
    await McpServer.create(name="linear", url="https://mcp.linear.app/sse", token=_TOKEN)
    druks_db.expunge_all()

    # Loading and logging a row never touches key material — decryption
    # happens only on decrypt(), and repr leaks nothing either way.
    row = await McpServer.get_for_name("linear")
    _set_key(monkeypatch, tmp_path, "")
    assert repr(row.token) == "Secret(<redacted>)"
    assert str(row.token) == "Secret(<redacted>)"
    with pytest.raises(ValidationError, match="Field required"):
        row.token.decrypt()


async def test_empty_value_needs_no_key(monkeypatch, tmp_path, druks_db):
    # "" stores as empty bytes — presence checks and decrypt() of an absent
    # secret never touch key material (proven by breaking the key first).
    await McpServer.create(name="linear", url="https://mcp.linear.app/sse", token="")
    druks_db.expunge_all()

    assert (
        bytes((await druks_db.execute(text("SELECT token FROM mcp_servers"))).scalar_one()) == b""
    )
    row = await McpServer.get_for_name("linear")
    _set_key(monkeypatch, tmp_path, "")
    assert not row.token
    assert row.token.decrypt() == ""


async def test_non_str_assignment_is_rejected(druks_db):
    server = await McpServer.create(name="linear", url="https://mcp.linear.app/sse", token=_TOKEN)

    server.token = 123
    with pytest.raises(StatementError, match="takes a str"):
        await db_session().flush()


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
    await McpServer.create(name="linear", url="https://mcp.linear.app/sse", token=_TOKEN)
    druks_db.expunge_all()
    _set_key(monkeypatch, tmp_path, _key())

    with pytest.raises(SecretDecryptError, match="rotated out"):
        (await McpServer.get_for_name("linear")).token.decrypt()


async def test_garbled_envelope_raises_the_named_error(druks_db):
    # No structural pre-checks in decrypt: GCM authentication (and the
    # ValueError a mangled nonce raises) fold every unreadable shape into the
    # one named error.
    await McpServer.create(name="linear", url="https://mcp.linear.app/sse", token=_TOKEN)
    await druks_db.execute(text(r"UPDATE mcp_servers SET token = '\x01ab'::bytea"))
    druks_db.expunge_all()

    with pytest.raises(SecretDecryptError):
        (await McpServer.get_for_name("linear")).token.decrypt()


async def test_ciphertext_is_bound_to_its_column(druks_db):
    # An envelope can't be replayed into any other encrypted column — not
    # another table's, and not a sibling column on the same row.
    await McpServer.create(name="linear", url="https://mcp.linear.app/sse", token=_TOKEN)
    await _store_grant(refresh_token="rt-secret", client_secret="cs-secret")
    await druks_db.execute(
        text(
            "UPDATE oauth_connections SET refresh_token ="
            " (SELECT token FROM mcp_servers WHERE name = 'linear')"
        )
    )
    await druks_db.execute(
        text(
            "UPDATE mcp_client_registrations SET client_secret ="
            " (SELECT refresh_token FROM oauth_connections WHERE provider = 'mcp:notion')"
        )
    )
    druks_db.expunge_all()

    grant = (await OauthConnection.list_for_account("mcp:notion", SYSTEM_ACCOUNT_ID))[0]
    registration = await McpClientRegistration.get_for_account("notion", SYSTEM_ACCOUNT_ID)
    with pytest.raises(SecretDecryptError):
        grant.refresh_token.decrypt()
    with pytest.raises(SecretDecryptError):
        registration.client_secret.decrypt()


async def test_prepended_key_still_decrypts(monkeypatch, tmp_path, druks_db):
    # Rotation is prepend-only: new writes use the first key; rows written
    # under an older key keep decrypting as long as it stays in the list.
    old_key = _key()
    _set_key(monkeypatch, tmp_path, old_key)
    await McpServer.create(name="linear", url="https://mcp.linear.app/sse", token=_TOKEN)
    await _store_grant(refresh_token="rt-secret")

    _set_key(monkeypatch, tmp_path, f"{_key()},{old_key}")
    druks_db.expunge_all()
    assert (await McpServer.get_for_name("linear")).token.decrypt() == _TOKEN
    assert (await OauthConnection.list_for_account("mcp:notion", SYSTEM_ACCOUNT_ID))[
        0
    ].refresh_token.decrypt() == "rt-secret"


# --- EncryptedJsonField (via the test-only model) ---------------------------


async def test_json_mapping_round_trips_as_ciphertext(druks_db):
    druks_db.add(EncryptedNote(data={"token": _TOKEN, "extra": "x"}))
    await druks_db.flush()

    blob = bytes(
        (await druks_db.execute(text("SELECT data FROM test_encrypted_notes"))).scalar_one()
    )
    assert _TOKEN.encode() not in blob
    druks_db.expunge_all()
    note = (await druks_db.execute(select(EncryptedNote))).scalar_one()
    assert note.data["token"] == _TOKEN
    assert repr(note.data) == "SecretsMapping(<redacted>)"


async def test_json_in_place_write_persists(druks_db):
    # Writing one key of the mapping must mark the column dirty on its own
    # (the Mutable wiring) and survive the flush.
    druks_db.add(EncryptedNote(data={"token": "old"}))
    await druks_db.flush()
    druks_db.expunge_all()

    note = (await druks_db.execute(select(EncryptedNote))).scalar_one()
    note.data["token"] = "new"
    await druks_db.flush()
    druks_db.expunge_all()

    assert (await druks_db.execute(select(EncryptedNote))).scalar_one().data["token"] == "new"


def test_json_non_dict_assignment_is_rejected(druks_db):
    note = EncryptedNote(data={"token": "t"})

    with pytest.raises(ValueError, match="dict"):
        note.data = "plaintext"
