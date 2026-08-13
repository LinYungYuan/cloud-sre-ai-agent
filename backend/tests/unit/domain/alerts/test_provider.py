from sre_agent.domain.alerts.cross_cloud import AlertValidationError
from sre_agent.domain.alerts.provider import Provider, detect_provider


def test_provider_is_exclusively_project_id_key_presence() -> None:
    gcp = detect_provider({"resource.label.project_id": "p-123"})
    aws = detect_provider({})

    assert (gcp.provider, gcp.project_id, gcp.errors) == (Provider.GCP, "p-123", ())
    assert (aws.provider, aws.project_id, aws.errors) == (Provider.AWS, None, ())


def test_present_blank_or_non_string_project_id_stays_gcp_and_is_invalid() -> None:
    for value in ("  ", None, 123, ["p-123"], {"project": "p-123"}):
        result = detect_provider({"resource.label.project_id": value})

        assert result.provider is Provider.GCP
        assert result.project_id is None
        assert result.errors == (
            AlertValidationError(
                field="resource.label.project_id",
                code="invalid_value",
            ),
        )


def test_conflicting_legacy_and_resource_fields_never_override_provider() -> None:
    distractions = {
        "cloud_provider": "gcp",
        "resource_id": "projects/p-123/locations/asia-east1",
        "Series": "123456789012",
        "DBInstanceIdentifier": "production-rds-01",
    }

    assert detect_provider(distractions).provider is Provider.AWS
    assert detect_provider(
        {**distractions, "resource.label.project_id": "p-123"}
    ).provider is Provider.GCP
