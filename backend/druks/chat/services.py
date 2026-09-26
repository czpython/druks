import httpx
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from druks.secrets.datastructures import Audience
from druks.secrets.models import VaultSecret
from druks.services import Service
from druks.services.exceptions import ServiceNotConnectedError

from .constants import TRANSCRIPTION_TIMEOUT_SECONDS
from .exceptions import TranscriptionError


class SpeechToText(Service):
    """The server Druks sends voice notes to: any server that speaks the OpenAI audio
    API, such as OpenAI, Groq, or a local one."""

    description = (
        "The service Druks sends voice notes to. Any server that speaks the OpenAI audio API."
    )
    required = False

    class Settings(BaseModel):
        url: str = Field(
            title="Address", description="Base URL, for example https://api.openai.com/v1."
        )
        key: SecretStr = Field(title="Key")
        model: str = Field(title="Model", description="For example whisper-1.")

    @classmethod
    async def transcribe(
        cls, session: AsyncSession, *, name: str, content_type: str, content: bytes
    ) -> str:
        """The words in an audio file. An empty answer is a failure."""
        card = await VaultSecret.lookup(session, cls.secret_kind, Audience.service(cls.slug))
        if not card:
            raise ServiceNotConnectedError(cls.slug)
        try:
            async with httpx.AsyncClient(timeout=TRANSCRIPTION_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    f"{card.identity['url'].rstrip('/')}/audio/transcriptions",
                    # A key pasted with a space would echo through the transport's error.
                    headers={"Authorization": f"Bearer {card.secrets['key'].strip()}"},
                    data={"model": card.identity["model"]},
                    files={"file": (name, content, content_type)},
                )
            response.raise_for_status()
            text = response.json()["text"].strip()
        except Exception as error:  # noqa: BLE001 — any transport or shape failure is a failed note
            raise TranscriptionError(f"{cls.title} gave no transcript: {error}") from error
        if not text:
            raise TranscriptionError(f"{cls.title} gave an empty transcript.")
        return text
