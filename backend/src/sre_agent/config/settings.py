from typing import Annotated
from uuid import UUID

from pydantic import (
    Field,
    SecretStr,
    StringConstraints,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

TokenId = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"),
]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=None,
        extra="forbid",
        hide_input_in_errors=True,
    )

    database_url: SecretStr
    grafana_tokens: dict[UUID, dict[TokenId, SecretStr]]
    pubsub_project_id: str
    rca_topic_id: str
    pubsub_emulator_host: str | None = None
    app_environment: str
    webhook_max_body_bytes: int = Field(default=1_048_576, ge=1024, le=1_048_576)

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().startswith("postgresql+asyncpg://"):
            raise ValueError("DATABASE_URL must use postgresql+asyncpg")
        return value

    @field_validator("grafana_tokens")
    @classmethod
    def validate_grafana_tokens(
        cls,
        value: dict[UUID, dict[str, SecretStr]],
    ) -> dict[UUID, dict[str, SecretStr]]:
        if not value:
            raise ValueError("GRAFANA_TOKENS must configure at least one source")
        for source_tokens in value.values():
            if not source_tokens:
                raise ValueError(
                    "each Grafana source must configure at least one token"
                )
            for token in source_tokens.values():
                credential = token.get_secret_value()
                if (
                    not credential
                    or not credential.isascii()
                    or any(character.isspace() for character in credential)
                ):
                    raise ValueError(
                        "Grafana token credentials must be non-empty ASCII without whitespace"
                    )
        return value

    @model_validator(mode="after")
    def reject_production_emulator(self) -> "Settings":
        if self.app_environment == "production" and self.pubsub_emulator_host:
            raise ValueError("PUBSUB_EMULATOR_HOST is forbidden in production")
        return self
