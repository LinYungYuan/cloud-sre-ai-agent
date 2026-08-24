import pytest
from pydantic import SecretStr, ValidationError

import sre_rca_worker.config.settings as settings_module
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


def test_pubsub_auto_create_defaults_to_false() -> None:
    assert _settings().pubsub_auto_create is False


def test_worker_id_defaults_to_the_runtime_hostname_when_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WORKER_ID", raising=False)
    monkeypatch.setattr(settings_module.socket, "gethostname", lambda: "rca-pod-7")

    assert _settings().worker_id == "rca-pod-7"


def test_worker_id_rejects_whitespace_only_values() -> None:
    with pytest.raises(ValidationError, match="WORKER_ID must not be blank"):
        _settings(worker_id="   ")


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


def test_specialist_analysis_defaults_to_disabled_with_bounded_budgets() -> None:
    settings = _settings()

    assert settings.specialist_analysis_mode.value == "DISABLED"
    assert settings.mcp_max_response_bytes == 2 * 1024 * 1024
    assert settings.evidence_chunk_chars == 8_000
    assert settings.evidence_max_chunks == 4
    assert settings.evidence_max_total_chars == 32_000
    assert settings.specialist_max_tool_calls == 5
    assert settings.specialist_max_observations == 20
    assert settings.rca_deadline_seconds == 300
    assert settings.agent_corrective_retries == 1
    assert (
        settings.evidence_chunk_chars * settings.evidence_max_chunks
        <= settings.evidence_max_total_chars
    )


def test_specialist_analysis_mode_rejects_lowercase_values() -> None:
    with pytest.raises(ValidationError, match="DISABLED.*SHADOW.*ACTIVE"):
        _settings(specialist_analysis_mode="disabled")


@pytest.mark.parametrize(
    "field_name",
    [
        "mcp_max_response_bytes",
        "evidence_chunk_chars",
        "evidence_max_chunks",
        "evidence_max_total_chars",
        "specialist_max_tool_calls",
        "specialist_max_observations",
        "rca_deadline_seconds",
    ],
)
def test_specialist_positive_budgets_reject_zero(field_name: str) -> None:
    with pytest.raises(ValidationError, match="greater than 0"):
        _settings(**{field_name: 0})


@pytest.mark.parametrize(
    ("field_name", "above_hard_cap"),
    [
        ("mcp_max_response_bytes", 2 * 1024 * 1024 + 1),
        ("evidence_chunk_chars", 8_001),
        ("evidence_max_chunks", 5),
        ("evidence_max_total_chars", 32_001),
        ("specialist_max_tool_calls", 6),
        ("specialist_max_observations", 21),
        ("rca_deadline_seconds", 301),
        ("agent_corrective_retries", 2),
    ],
)
def test_production_rejects_specialist_budget_increases(
    field_name: str, above_hard_cap: int
) -> None:
    with pytest.raises(ValidationError, match="less than or equal to"):
        _settings(
            app_environment="production",
            pubsub_emulator_host=None,
            **{field_name: above_hard_cap},
        )


def test_evidence_budgets_reject_a_combined_total_above_the_limit() -> None:
    with pytest.raises(
        ValidationError,
        match="evidence chunk budget exceeds total character limit",
    ):
        _settings(evidence_max_total_chars=31_999)


def test_agent_corrective_retries_allows_zero() -> None:
    assert _settings(agent_corrective_retries=0).agent_corrective_retries == 0
