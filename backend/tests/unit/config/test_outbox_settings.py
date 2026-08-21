import pytest
from pydantic import ValidationError

from sre_agent.config.outbox_settings import OutboxSettings

VALID_PAYLOAD = {
    "database_url": "postgresql+asyncpg://app:database-password@db/sre",
    "pubsub_project_id": "local-project",
    "rca_topic_id": "rca-jobs",
}


def test_outbox_settings_validates_required_runtime_fields() -> None:
    settings = OutboxSettings.model_validate(VALID_PAYLOAD)

    assert settings.database_url.get_secret_value() == (
        "postgresql+asyncpg://app:database-password@db/sre"
    )
    assert settings.pubsub_project_id == "local-project"
    assert settings.rca_topic_id == "rca-jobs"


@pytest.mark.parametrize("missing_field", VALID_PAYLOAD)
def test_outbox_settings_requires_each_runtime_field(missing_field: str) -> None:
    payload = {
        key: value for key, value in VALID_PAYLOAD.items() if key != missing_field
    }

    with pytest.raises(ValidationError):
        OutboxSettings.model_validate(payload)


@pytest.mark.parametrize(
    ("extra_field", "extra_value"),
    [
        ("grafana_tokens", {}),
        ("model_name", "test-model"),
        ("metrics_mcp_url", "https://gateway/metrics/mcp"),
    ],
)
def test_outbox_settings_rejects_backend_only_runtime_fields(
    extra_field: str,
    extra_value: object,
) -> None:
    payload = {**VALID_PAYLOAD, extra_field: extra_value}

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        OutboxSettings.model_validate(payload)


def test_outbox_settings_validation_error_does_not_leak_database_password() -> None:
    database_password = "database-password-that-must-not-leak"
    payload = {
        **VALID_PAYLOAD,
        "database_url": f"postgresql://app:{database_password}@db/sre",
    }

    with pytest.raises(ValidationError) as error:
        OutboxSettings.model_validate(payload)

    assert database_password not in str(error.value)
