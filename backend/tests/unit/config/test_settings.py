from pathlib import Path

import pytest
from pydantic import ValidationError

from sre_agent.api.main import _load_settings
from sre_agent.config.settings import Settings


def _settings_values(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "database_url": "postgresql+asyncpg://app:test@db/sre",
        "grafana_tokens": {
            "50000000-0000-0000-0000-000000000001": {"current": "secret"}
        },
        "pubsub_project_id": "local-project",
        "rca_topic_id": "rca-jobs",
        "app_environment": "test",
    }
    values.update(overrides)
    return values


def _clear_backend_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "DATABASE_URL",
        "GRAFANA_TOKENS",
        "PUBSUB_PROJECT_ID",
        "RCA_TOPIC_ID",
        "PUBSUB_EMULATOR_HOST",
        "APP_ENVIRONMENT",
        "WEBHOOK_MAX_BODY_BYTES",
    ):
        monkeypatch.delenv(key, raising=False)


def test_backend_settings_exposes_only_backend_runtime_fields() -> None:
    assert set(Settings.model_fields) == {
        "database_url",
        "grafana_tokens",
        "pubsub_project_id",
        "rca_topic_id",
        "pubsub_emulator_host",
        "app_environment",
        "webhook_max_body_bytes",
    }


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("model_name", "test-model"),
        ("metrics_mcp_url", "https://gateway/metrics"),
        ("trace_mcp_url", "https://gateway/traces"),
        ("log_mcp_url", "https://gateway/logs"),
        ("mcp_max_response_bytes", 1),
        ("evidence_chunk_chars", 1),
        ("evidence_max_chunks", 1),
        ("evidence_max_total_chars", 1),
        ("specialist_max_tool_calls", 1),
        ("specialist_max_observations", 1),
        ("rca_deadline_seconds", 1),
        ("agent_corrective_retries", 1),
    ],
)
def test_backend_settings_rejects_worker_only_fields(
    field_name: str, value: object
) -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Settings.model_validate(_settings_values(**{field_name: value}))


def test_backend_settings_rejects_pubsub_emulator_in_production() -> None:
    with pytest.raises(ValidationError, match="forbidden in production"):
        Settings.model_validate(
            _settings_values(
                app_environment="production",
                pubsub_emulator_host="127.0.0.1:58085",
            )
        )


def test_backend_loader_reads_dedicated_env_file_with_os_environment_precedence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_backend_environment(monkeypatch)
    env_file = tmp_path / ".env.backend-api"
    env_file.write_text(
        """DATABASE_URL=postgresql+asyncpg://file:file@db/sre
GRAFANA_TOKENS={"50000000-0000-0000-0000-000000000001":{"current":"file-token"}}
PUBSUB_PROJECT_ID=file-project
RCA_TOPIC_ID=file-topic
APP_ENVIRONMENT=local
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("BACKEND_ENV_FILE", str(env_file))
    monkeypatch.setenv("APP_ENVIRONMENT", "test")

    settings = _load_settings()

    assert settings.pubsub_project_id == "file-project"
    assert settings.app_environment == "test"


def test_settings_reads_explicit_environment(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://app:test@db/sre")
    monkeypatch.setenv(
        "GRAFANA_TOKENS",
        '{"50000000-0000-0000-0000-000000000001":{"current":"secret"}}',
    )
    monkeypatch.setenv("PUBSUB_PROJECT_ID", "local-project")
    monkeypatch.setenv("RCA_TOPIC_ID", "rca-jobs")
    monkeypatch.setenv("APP_ENVIRONMENT", "test")

    settings = Settings()  # pyright: ignore[reportCallIssue]

    assert settings.app_environment == "test"
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

    with pytest.raises(ValidationError) as error:
        Settings()  # pyright: ignore[reportCallIssue]

    assert credential not in str(error.value)
