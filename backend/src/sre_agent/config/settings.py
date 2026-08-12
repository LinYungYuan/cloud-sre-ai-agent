from pydantic import AnyHttpUrl, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="forbid")

    database_url: str
    pubsub_project_id: str
    rca_topic_id: str
    app_environment: str
    model_name: str
    metrics_mcp_url: AnyHttpUrl
    trace_mcp_url: AnyHttpUrl
    log_mcp_url: AnyHttpUrl
    rca_deadline_seconds: int = Field(default=300, ge=60, le=300)
    webhook_max_body_bytes: int = Field(default=1_048_576, ge=1024)
