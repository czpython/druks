from unittest.mock import AsyncMock

import httpx
import pytest
from calls.app import app
from calls.call import get_model
from fastapi.testclient import TestClient
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.services.google.gemini_live.llm import GeminiLiveLLMService
from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService
from starlette.websockets import WebSocketDisconnect


def test_a_pickup_that_druks_refuses_refuses_the_stream(monkeypatch):
    monkeypatch.setenv("DRUKS_URL", "http://druks.test")
    post = AsyncMock(return_value=httpx.Response(401))
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    headers = {"X-Twilio-Signature": "signed"}

    with (
        TestClient(app) as client,
        pytest.raises(WebSocketDisconnect) as refusal,
        client.websocket_connect("/_calls/a-token", headers=headers),
    ):
        pass

    assert refusal.value.code == 1008
    pickup = {"action": "pickup", "token": "a-token", "signature": "signed"}
    assert post.await_args.kwargs["json"] == pickup


@pytest.mark.parametrize(
    "model, service",
    [
        ("openai/gpt-realtime-mini", OpenAIRealtimeLLMService),
        ("google/gemini-2.5-flash-native-audio", GeminiLiveLLMService),
    ],
)
def test_the_models_vendor_picks_its_service(model, service):
    pickup = {
        "voice": {
            "model": model,
            "key": "key",
            "voice_name": "",
            "transcription_model": "gpt-4o-mini-transcribe",
        },
        "prompt": "Help with tickets.",
        "facts": {},
        "limits": {"no_speech_seconds": 15},
    }

    model_service, _, _ = get_model(pickup=pickup, tools=ToolsSchema(standard_tools=[]))

    assert isinstance(model_service, service)
