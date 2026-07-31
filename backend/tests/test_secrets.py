import base64
import os

import pytest
from druks.accounts.constants import SYSTEM_ACCOUNT_ID
from druks.core.models import Uuid7Pk
from druks.mcp.models import McpOauthGrant, McpServer
from druks.models import Base
from druks.secrets.exceptions import SecretDecryptError
from druks.secrets.fields import EncryptedJsonField
from druks.settings import load_settings
from pydantic import ValidationError
from sqlalchemy import text
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


def _store_grant(refresh_token: str = "rt-secret", client_secret: str = "") -> McpOauthGrant:
    return McpOauthGrant.store(
        server_name="notion",
        account_id=SYSTEM_ACCOUNT_ID,
        refresh_token=refresh_token,
        token_endpoint="https://auth.test/token",
        resource="https://mcp.notion.test/sse",
        client_id="client-123",
        client_secret=client_secret,
    )


def test_stored_secrets_are_ciphertext_and_reads_restore_them(druks_db):
    McpServer.create(name="linear", url="https://mcp.linear.app/sse", token=_TOKEN)

    blob = bytes(druks_db.execute(text("SELECT token FROM mcp_servers")).scalar_one())
    assert _TOKEN.encode() not in blob
    druks_db.expire_all()
    row = McpServer.get_for_name("linear")
    assert row.token.decrypt() == _TOKEN
    # The merged view every consumer reads carries the Secret itself, so the
    # plaintext exists only where decrypt() is called.
    merged = McpServer._merged()["linear"]
    assert merged["token"].decrypt() == _TOKEN


def test_grant_secret_halves_round_trip(druks_db):
    _store_grant(refresh_token="rt-secret", client_secret="cs-secret")

    druks_db.expire_all()
    grant = McpOauthGrant.get_for_scope("notion", SYSTEM_ACCOUNT_ID)
    assert grant.refresh_token.decrypt() == "rt-secret"
    assert grant.client_secret.decrypt() == "cs-secret"


def test_loaded_secrets_are_lazy_and_redacted(monkeypatch, druks_db):
    McpServer.create(name="linear", url="https://mcp.linear.app/sse", token=_TOKEN)
    druks_db.expire_all()

    # Loading and logging a row never touches key material — decryption
    # happens only on decrypt(), and repr leaks nothing either way.
    row = McpServer.get_for_name("linear")
    monkeypatch.setenv("DRUKS_SECRETS_KEY", "")
    assert repr(row.token) == "Secret(<redacted>)"
    assert str(row.token) == "Secret(<redacted>)"
    with pytest.raises(ValidationError, match="at least one"):
        row.token.decrypt()


def test_empty_value_needs_no_key(monkeypatch, druks_db):
    # "" stores as empty bytes — presence checks and decrypt() of an absent
    # secret never touch key material (proven by breaking the key first).
    McpServer.create(name="linear", url="https://mcp.linear.app/sse", token="")
    druks_db.expire_all()

    assert bytes(druks_db.execute(text("SELECT token FROM mcp_servers")).scalar_one()) == b""
    row = McpServer.get_for_name("linear")
    monkeypatch.setenv("DRUKS_SECRETS_KEY", "")
    assert not row.token
    assert row.token.decrypt() == ""


def test_non_str_assignment_is_rejected(druks_db):
    server = McpServer.create(name="linear", url="https://mcp.linear.app/sse", token=_TOKEN)

    server.token = 123
    with pytest.raises(StatementError, match="takes a str"):
        druks_db.flush()


def test_missing_key_refuses_boot(monkeypatch):
    # Blank and comma-noise-only both read as "no key" — the required setting
    # refuses at construction rather than falling back to plaintext.
    for broken in ("", ",", " , "):
        monkeypatch.setenv("DRUKS_SECRETS_KEY", broken)
        with pytest.raises(ValidationError, match="at least one"):
            load_settings()


def test_key_validation_error_never_echoes_the_key(monkeypatch):
    # A half-valid list fails validation, and the failure surfaces in boot
    # logs and doctor output — it must not echo the valid segment.
    good = _key()
    monkeypatch.setenv("DRUKS_SECRETS_KEY", f"{good},not-base64!!")

    with pytest.raises(ValidationError) as error_info:
        load_settings()
    assert good not in str(error_info.value)


def test_malformed_key_refuses_boot(monkeypatch):
    for broken in ("not-base64!!", base64.b64encode(b"short").decode()):
        monkeypatch.setenv("DRUKS_SECRETS_KEY", broken)
        with pytest.raises(ValidationError, match="base64|32 bytes"):
            load_settings()


def test_undecryptable_secret_raises_the_named_error(monkeypatch, druks_db):
    # A key dropped from the list while rows written under it existed is the
    # usual cause — the error must say so, not surface a bare crypto traceback.
    McpServer.create(name="linear", url="https://mcp.linear.app/sse", token=_TOKEN)
    druks_db.expire_all()
    monkeypatch.setenv("DRUKS_SECRETS_KEY", _key())

    with pytest.raises(SecretDecryptError, match="rotated out"):
        McpServer.get_for_name("linear").token.decrypt()


def test_garbled_envelope_raises_the_named_error(druks_db):
    # No structural pre-checks in decrypt: GCM authentication (and the
    # ValueError a mangled nonce raises) fold every unreadable shape into the
    # one named error.
    McpServer.create(name="linear", url="https://mcp.linear.app/sse", token=_TOKEN)
    druks_db.execute(text(r"UPDATE mcp_servers SET token = '\x01ab'::bytea"))
    druks_db.expire_all()

    with pytest.raises(SecretDecryptError):
        McpServer.get_for_name("linear").token.decrypt()


def test_ciphertext_is_bound_to_its_column(druks_db):
    # An envelope can't be replayed into any other encrypted column — not
    # another table's, and not a sibling column on the same row.
    McpServer.create(name="linear", url="https://mcp.linear.app/sse", token=_TOKEN)
    _store_grant(refresh_token="rt-secret", client_secret="cs-secret")
    druks_db.execute(
        text("UPDATE mcp_oauth_grants SET refresh_token = (SELECT token FROM mcp_servers)")
    )
    druks_db.execute(text("UPDATE mcp_oauth_grants SET client_secret = refresh_token"))
    druks_db.expire_all()

    grant = McpOauthGrant.get_for_scope("notion", SYSTEM_ACCOUNT_ID)
    with pytest.raises(SecretDecryptError):
        grant.refresh_token.decrypt()
    with pytest.raises(SecretDecryptError):
        grant.client_secret.decrypt()


def test_prepended_key_still_decrypts(monkeypatch, druks_db):
    # Rotation is prepend-only: new writes use the first key; rows written
    # under an older key keep decrypting as long as it stays in the list.
    old_key = _key()
    monkeypatch.setenv("DRUKS_SECRETS_KEY", old_key)
    McpServer.create(name="linear", url="https://mcp.linear.app/sse", token=_TOKEN)
    _store_grant(refresh_token="rt-secret")

    monkeypatch.setenv("DRUKS_SECRETS_KEY", f"{_key()},{old_key}")
    druks_db.expire_all()
    assert McpServer.get_for_name("linear").token.decrypt() == _TOKEN
    assert (
        McpOauthGrant.get_for_scope("notion", SYSTEM_ACCOUNT_ID).refresh_token.decrypt()
        == "rt-secret"
    )


# --- EncryptedJsonField (via the test-only model) ---------------------------


def test_json_mapping_round_trips_as_ciphertext(druks_db):
    druks_db.add(EncryptedNote(data={"token": _TOKEN, "extra": "x"}))
    druks_db.flush()

    blob = bytes(druks_db.execute(text("SELECT data FROM test_encrypted_notes")).scalar_one())
    assert _TOKEN.encode() not in blob
    druks_db.expire_all()
    note = druks_db.query(EncryptedNote).one()
    assert note.data["token"] == _TOKEN
    assert repr(note.data) == "SecretsMapping(<redacted>)"


def test_json_in_place_write_persists(druks_db):
    # Writing one key of the mapping must mark the column dirty on its own
    # (the Mutable wiring) and survive the flush.
    druks_db.add(EncryptedNote(data={"token": "old"}))
    druks_db.flush()
    druks_db.expire_all()

    note = druks_db.query(EncryptedNote).one()
    note.data["token"] = "new"
    druks_db.flush()
    druks_db.expire_all()

    assert druks_db.query(EncryptedNote).one().data["token"] == "new"


def test_json_non_dict_assignment_is_rejected(druks_db):
    note = EncryptedNote(data={"token": "t"})

    with pytest.raises(ValueError, match="dict"):
        note.data = "plaintext"
