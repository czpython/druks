from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints

from druks.schemas import BaseResponse

NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class McpServerResponse(BaseResponse):
    # A pure projection of one ``McpServer.get_resolved()`` item — the dict's
    # ``token`` is not a field here, so the secret can't serialize (and it
    # arrives as a Secret, redacted even if it did).
    name: str
    url: str
    is_enabled: bool
    token_source: str
    builtin: bool
    # The deployment env var an env-sourced server reads its token from
    # ("" otherwise) — a var name, never a value.
    source_env_var: str
    # Whether the server can authenticate at delivery — never the token itself.
    has_token: bool


class ConnectMcpServerResponse(BaseResponse):
    # The consent URL the operator's browser opens; the grant lands via the
    # callback, never through this response.
    authorization_url: str


class McpRegistryCandidateResponse(BaseResponse):
    name: str
    registry_name: str
    description: str
    url: str
    official: bool
    # The remote's declared inputs, verbatim — the registry owns their shape.
    headers: list[dict]


class CreateMcpServerRequest(BaseModel):
    name: str
    url: str
    token: str = ""


class InstallMcpServerRequest(BaseModel):
    name: str
    registry: str
    headers: dict[str, str] = {}


# The catalog file is operator input, so its entries parse through a
# discriminated union — each auth strategy carries exactly its own fields, and
# an unknown key is rejected (a stray key in a catalog is a typo, not
# ecosystem noise: entries are druks-shaped and never paste in unchanged).


class StaticAuth(BaseModel):
    # The operator supplies the token via the server's overlay row.
    model_config = {"extra": "forbid"}
    type: Literal["static"]

    @property
    def source_env_var(self) -> str:
        return ""


class EnvAuth(BaseModel):
    # Delivery reads the token from a named var in druks' own process env.
    model_config = {"extra": "forbid"}
    type: Literal["static_from_env"]
    env: NonBlank

    @property
    def source_env_var(self) -> str:
        return self.env


class OauthAuth(BaseModel):
    # The operator connects the server once (consent → stored grant); delivery
    # mints a short-lived access token from the grant per run.
    model_config = {"extra": "forbid"}
    type: Literal["oauth"]

    @property
    def source_env_var(self) -> str:
        return ""


class CatalogEntry(BaseModel):
    model_config = {"extra": "forbid"}
    url: NonBlank
    transport: Literal["http"] = "http"
    auth: Annotated[StaticAuth | EnvAuth | OauthAuth, Field(discriminator="type")]
    # A catalog can ship a server dark — visible in settings, delivered to no
    # run until the operator turns it on (an oauth entry is unconnectable
    # before its first consent, so enabled-by-default would break every run).
    enabled: bool = True
