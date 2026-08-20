from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class OutboxSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=None,
        extra="forbid",
        hide_input_in_errors=True,
    )

    database_url: SecretStr
    pubsub_project_id: str
    rca_topic_id: str

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().startswith("postgresql+asyncpg://"):
            raise ValueError("DATABASE_URL must use postgresql+asyncpg")
        return value
