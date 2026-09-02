import pytest
from druks.accounts.models import Account
from druks.harnesses.claude import ClaudeHarness
from druks.harnesses.codex import CodexHarness
from druks.harnesses.exceptions import HarnessError
from druks.harnesses.registry import get_harness_for_model
from druks.user_settings.models import HarnessSettings
from druks.user_settings.routes import _validate_model, update_harness_settings
from druks.user_settings.schemas import HarnessUpdate
from fastapi import HTTPException


async def test_shipped_tuple_fallback_routes_shipped_models(druks_db):
    assert await get_harness_for_model("claude-opus-4-7") is ClaudeHarness
    assert await get_harness_for_model("gpt-5.5") is CodexHarness


async def test_fetched_list_routes_provider_models(druks_db):
    (await HarnessSettings.get_registered("claude")).models_fetched = [
        {"id": "claude-fable-5", "label": "Claude Fable 5"}
    ]
    await druks_db.flush()

    assert await get_harness_for_model("claude-fable-5") is ClaudeHarness


async def test_bare_harness_name_routes(druks_db):
    assert await get_harness_for_model("claude") is ClaudeHarness
    assert await get_harness_for_model("codex") is CodexHarness


async def test_unknown_model_raises_harness_error(druks_db):
    with pytest.raises(HarnessError):
        await get_harness_for_model("llama-3-70b")
    with pytest.raises(HarnessError):
        await get_harness_for_model("claude-opus-99")


async def test_settings_reject_model_missing_from_lists_returns_422(druks_db):
    with pytest.raises(HTTPException) as error:
        await _validate_model("llama-3-70b")

    assert error.value.status_code == 422
    assert error.value.detail == "No installed harness runs model 'llama-3-70b'."


@pytest.mark.parametrize(
    ("model", "detail"),
    [
        ("gpt-5.5", "'gpt-5.5' is not a claude model."),
        ("llama-3-70b", "'llama-3-70b' is not a claude model."),
    ],
)
async def test_settings_reject_invalid_model_returns_422(druks_db, model, detail):
    account = await Account.get_or_create("op@example.com")
    settings = await HarnessSettings.get_registered("claude")
    original_model = settings.model

    with pytest.raises(HTTPException) as error:
        await update_harness_settings(
            name="claude",
            body=HarnessUpdate(model=model),
            account=account,
        )

    assert error.value.status_code == 422
    assert error.value.detail == detail
    assert settings.model == original_model
