import pytest
from pydantic import SecretStr, ValidationError

from sre_rca_worker.config.settings import WorkerSettings


def _settings(**overrides):
    values = {
        "database_url": SecretStr("postgresql+asyncpg://app@db/sre"),
        "pubsub_project_id": "sre-agent-local",
        "rca_topic_id": "rca-jobs",
        "pubsub_subscription_id": "rca-worker",
        "app_environment": "local",
        "model_name": "test-model",
        "pubsub_emulator_host": "127.0.0.1:58085",
    }
    values.update(overrides)
    return WorkerSettings.model_validate(values)


def test_local_settings_allow_the_official_emulator_without_credentials() -> None:
    settings = _settings()

    assert settings.pubsub_emulator_host == "127.0.0.1:58085"
    assert "app@db" not in repr(settings)


def test_production_rejects_an_emulator_host() -> None:
    with pytest.raises(ValidationError, match="forbidden in production"):
        _settings(app_environment="production")


def test_manifest_is_loaded_only_from_validated_startup_configuration() -> None:
    settings = _settings(
        mcp_capability_manifest=[
            {
                "endpoint_identity": "metrics",
                "capability": "metrics.query",
                "tool_name_pattern": "^metrics_query$",
                "input_schema": {
                    "type": "object",
                    "properties": {"project_id": {"type": "string"}},
                    "required": ["project_id"],
                    "additionalProperties": False,
                },
                "risk": "READ_ONLY",
            }
        ]
    )
    entry = settings.mcp_capability_manifest[0]
    assert entry.endpoint_identity == "metrics"
    assert len(entry.input_schema_hash or "") == 64
