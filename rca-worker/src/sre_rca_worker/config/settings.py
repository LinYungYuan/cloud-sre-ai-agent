from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from sre_rca_worker.integrations.mcp.models import ManifestEntry


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=None,
        extra="forbid",
        hide_input_in_errors=True,
    )

    database_url: SecretStr
    pubsub_project_id: str = Field(min_length=1)
    rca_topic_id: str = Field(min_length=1)
    pubsub_subscription_id: str = Field(min_length=1)
    app_environment: str = Field(min_length=1)
    model_name: str = Field(min_length=1)
    pubsub_emulator_host: str | None = None
    mcp_capability_manifest: tuple[ManifestEntry, ...] = ()
    metrics_mcp_url: str = (
        "https://agentgateway.cp.gcubut.gcp.uwccb/agw/gcp-metrics-mcp"
    )
    trace_mcp_url: str = "https://agentgateway.cp.gcubut.gcp.uwccb/agw/gcp-trace-mcp"
    log_mcp_url: str = "https://agentgateway.cp.gcubut.gcp.uwccb/agw/gcp-log-mcp"

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().startswith("postgresql+asyncpg://"):
            raise ValueError("DATABASE_URL must use postgresql+asyncpg")
        return value

    @model_validator(mode="after")
    def reject_production_emulator(self) -> "WorkerSettings":
        if self.app_environment == "production" and self.pubsub_emulator_host:
            raise ValueError("PUBSUB_EMULATOR_HOST is forbidden in production")
        return self

    @field_validator("metrics_mcp_url", "trace_mcp_url", "log_mcp_url")
    @classmethod
    def validate_mcp_url(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise ValueError("MCP endpoint must use HTTPS")
        return value

    @property
    def mcp_headers(self) -> dict[str, str]:
        """The approved MCP gateways currently use no authentication material."""
        return {}
