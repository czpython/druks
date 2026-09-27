from pydantic import BaseModel, Field, SecretStr

from druks.services import Service, ServiceConnectError


class Voice(Service):
    description = "The voice model that answers phone calls."
    required = False

    class Settings(BaseModel):
        model: str = Field(
            title="Model",
            description=(
                "The vendor and the model, for example openai/gpt-realtime-mini or "
                "google/gemini-2.5-flash-native-audio."
            ),
        )
        key: SecretStr = Field(title="Key")
        voice_name: str = Field(
            "",
            title="Voice name",
            description="The vendor's voice, for example marin or Kore. Empty uses the default.",
        )
        transcription_model: str = Field(
            "gpt-4o-mini-transcribe",
            title="Transcription model",
            description=(
                "Only an OpenAI model uses it. It writes the caller's words as text. Empty "
                "uses gpt-4o-mini-transcribe."
            ),
        )

    @classmethod
    async def verify(cls, settings: Settings) -> dict:
        if settings.model.partition("/")[0] in ("openai", "google"):
            return {}
        raise ServiceConnectError(
            "The calls server runs OpenAI and Google models. Write the model as "
            "openai/<model> or google/<model>."
        )
