import socket
from enum import StrEnum

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from sre_rca_worker.integrations.mcp.models import ManifestEntry


def _default_worker_id() -> str:
    return socket.gethostname()


class SpecialistAnalysisMode(StrEnum):
    DISABLED = "DISABLED"
    SHADOW = "SHADOW"
    ACTIVE = "ACTIVE"


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
    pubsub_auto_create: bool = False
    worker_id: str = Field(default_factory=_default_worker_id, min_length=1)
    mcp_capability_manifest: tuple[ManifestEntry, ...] = ()
    metrics_mcp_url: str = (
        "https://agentgateway.cp.gcubut.gcp.uwccb/agw/gcp-metrics-mcp"
    )
    trace_mcp_url: str = "https://agentgateway.cp.gcubut.gcp.uwccb/agw/gcp-trace-mcp"
    log_mcp_url: str = "https://agentgateway.cp.gcubut.gcp.uwccb/agw/gcp-log-mcp"
    specialist_analysis_mode: SpecialistAnalysisMode = SpecialistAnalysisMode.DISABLED
    mcp_max_response_bytes: int = Field(
        default=2 * 1024 * 1024, gt=0, le=2 * 1024 * 1024
    )
    evidence_chunk_chars: int = Field(default=8_000, gt=0, le=8_000)
    evidence_max_chunks: int = Field(default=4, gt=0, le=4)
    evidence_max_total_chars: int = Field(default=32_000, gt=0, le=32_000)
    specialist_max_tool_calls: int = Field(default=5, gt=0, le=5)
    specialist_max_observations: int = Field(default=20, gt=0, le=20)
    rca_deadline_seconds: int = Field(default=300, gt=0, le=300)
    agent_corrective_retries: int = Field(default=1, ge=0, le=1)

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().startswith("postgresql+asyncpg://"):
            raise ValueError("DATABASE_URL must use postgresql+asyncpg")
        return value

    @field_validator("worker_id")
    @classmethod
    def validate_worker_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("WORKER_ID must not be blank")
        return value

    @model_validator(mode="after")
    def reject_production_emulator(self) -> "WorkerSettings":
        if self.app_environment == "production" and self.pubsub_emulator_host:
            raise ValueError("PUBSUB_EMULATOR_HOST is forbidden in production")
        return self

    @model_validator(mode="after")
    def validate_evidence_budget(self) -> "WorkerSettings":
        if (
            self.evidence_max_total_chars
            > self.evidence_chunk_chars * self.evidence_max_chunks
        ):
            raise ValueError("evidence total character limit exceeds chunk capacity")
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
