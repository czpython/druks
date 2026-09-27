from pydantic import BaseModel, Field, SecretStr

from druks.services import Service


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
