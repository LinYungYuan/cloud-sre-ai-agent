import pytest
from pydantic import ValidationError

from sre_agent.config.settings import Settings


def test_settings_reads_explicit_environment(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://app:test@db/sre")
    monkeypatch.setenv(
        "GRAFANA_TOKENS",
        '{"50000000-0000-0000-0000-000000000001":{"current":"secret"}}',
    )
    monkeypatch.setenv("PUBSUB_PROJECT_ID", "local-project")
    monkeypatch.setenv("RCA_TOPIC_ID", "rca-jobs")
    monkeypatch.setenv("APP_ENVIRONMENT", "test")
    monkeypatch.setenv("MODEL_NAME", "test-model")
    monkeypatch.setenv("METRICS_MCP_URL", "https://gateway/gcp/metrics/mcp")
    monkeypatch.setenv("TRACE_MCP_URL", "https://gateway/gcp/trace/mcp")
    monkeypatch.setenv("LOG_MCP_URL", "https://gateway/gcp/log/mcp")

    settings = Settings()  # pyright: ignore[reportCallIssue]

    assert settings.app_environment == "test"
    assert settings.rca_deadline_seconds == 300
    assert "secret" not in repr(settings)


def test_invalid_environment_token_does_not_leak_credential(
    monkeypatch: pytest.MonkeyPatch,
):
    credential = "do-not-leak token"
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://app:test@db/sre")
    monkeypatch.setenv(
        "GRAFANA_TOKENS",
        (f'{{"50000000-0000-0000-0000-000000000001":{{"current":"{credential}"}}}}'),
    )
    monkeypatch.setenv("PUBSUB_PROJECT_ID", "local-project")
    monkeypatch.setenv("RCA_TOPIC_ID", "rca-jobs")
    monkeypatch.setenv("APP_ENVIRONMENT", "test")
    monkeypatch.setenv("MODEL_NAME", "test-model")
    monkeypatch.setenv("METRICS_MCP_URL", "https://gateway/gcp/metrics/mcp")
    monkeypatch.setenv("TRACE_MCP_URL", "https://gateway/gcp/trace/mcp")
    monkeypatch.setenv("LOG_MCP_URL", "https://gateway/gcp/log/mcp")

    with pytest.raises(ValidationError) as error:
        Settings()  # pyright: ignore[reportCallIssue]

    assert credential not in str(error.value)
