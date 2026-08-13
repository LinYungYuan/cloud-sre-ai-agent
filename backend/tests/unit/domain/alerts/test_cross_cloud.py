from dataclasses import FrozenInstanceError
from uuid import UUID

import pytest

from sre_agent.domain.alerts.cross_cloud import (
    AlertValidationError,
    CrossCloudAlertValidator,
    make_incident_identity,
)

SOURCE_ONE = UUID("00000000-0000-0000-0000-000000000001")
SOURCE_TWO = UUID("00000000-0000-0000-0000-000000000002")

GCP_LABELS = {
    "alertname": "GkePodRestart",
    "cloud_provider": "gcp",
    "cloud_scope_id": "svc-lx-afa-01-uat-1b9a87",
    "resource_type": "gke_pod",
    "resource_id": "projects/svc-lx-afa-01-uat-1b9a87/locations/asia-east1/clusters/cluster-a/namespaces/default/pods/aaaa-7b9f",
    "environment": "uat",
    "service": "aaaa",
    "team": "platform",
    "severity": "warning",
    "signal_type": "metric",
}

AWS_LABELS = {
    "alertname": "RdsCpuHigh",
    "cloud_provider": "aws",
    "cloud_scope_id": "123456789012",
    "resource_type": "rds_instance",
    "resource_id": "arn:aws:rds:ap-northeast-1:123456789012:db:orders-prod",
    "environment": "prod",
    "service": "orders",
    "team": "commerce",
    "severity": "critical",
    "signal_type": "metric",
}


@pytest.mark.parametrize("labels", [GCP_LABELS, AWS_LABELS])
def test_validator_accepts_complete_gcp_and_aws_labels(labels: dict[str, str]):
    result = CrossCloudAlertValidator().validate(labels)

    assert result.is_valid is True
    assert result.errors == ()


@pytest.mark.parametrize(
    "field",
    (
        "alertname",
        "cloud_provider",
        "cloud_scope_id",
        "resource_type",
        "resource_id",
        "environment",
        "service",
        "team",
        "severity",
        "signal_type",
    ),
)
@pytest.mark.parametrize("value", [None, "   "], ids=["missing", "blank"])
def test_validator_reports_required_error_for_each_missing_or_blank_label(
    field: str, value: str | None
):
    labels = dict(GCP_LABELS)
    if value is None:
        labels.pop(field)
    else:
        labels[field] = value

    result = CrossCloudAlertValidator().validate(labels)

    assert result.is_valid is False
    assert result.errors == (AlertValidationError(field=field, code="required"),)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("cloud_provider", "azure"),
        ("severity", "urgent"),
        ("signal_type", "event"),
    ],
)
def test_validator_reports_invalid_values_for_enumerated_labels(field: str, value: str):
    labels = dict(GCP_LABELS, **{field: value})

    result = CrossCloudAlertValidator().validate(labels)

    assert result.is_valid is False
    assert result.errors == (AlertValidationError(field=field, code="invalid_value"),)


@pytest.mark.parametrize(
    "resource_type",
    ["GKE_POD", "gke-pod", "gke__pod", "_gke_pod", "gke_pod_", "gke pod"],
)
def test_validator_rejects_noncanonical_resource_type_tokens(resource_type: str):
    result = CrossCloudAlertValidator().validate(
        dict(GCP_LABELS, resource_type=resource_type)
    )

    assert result.is_valid is False
    assert result.errors == (
        AlertValidationError(field="resource_type", code="invalid_value"),
    )


@pytest.mark.parametrize(
    "resource_id",
    [
        "svc-lx-afa-01-uat-1b9a87",
        "projects/svc-lx-afa-01-uat-1b9a87",
        "projects/svc-lx-afa-01-uat-1b9a87/",
        "projects/other-project/locations/asia-east1/services/api",
        "projects/svc-lx-afa-01-uat-1b9a87/services",
    ],
)
def test_validator_rejects_incomplete_or_cross_scope_gcp_resource_names(
    resource_id: str,
):
    result = CrossCloudAlertValidator().validate(
        dict(GCP_LABELS, resource_id=resource_id)
    )

    assert result.is_valid is False
    assert result.errors == (
        AlertValidationError(field="resource_id", code="invalid_value"),
    )


@pytest.mark.parametrize(
    "resource_id",
    [
        "123456789012",
        "arn:aws:rds:ap-northeast-1:123456789012:",
        "arn:aws:rds:ap-northeast-1:210987654321:db:orders-prod",
        "arn:aws:rds:ap-northeast-1::db:orders-prod",
        "rds:ap-northeast-1:123456789012:db:orders-prod",
    ],
)
def test_validator_rejects_incomplete_or_cross_account_aws_resource_ids(
    resource_id: str,
):
    result = CrossCloudAlertValidator().validate(
        dict(AWS_LABELS, resource_id=resource_id)
    )

    assert result.is_valid is False
    assert result.errors == (
        AlertValidationError(field="resource_id", code="invalid_value"),
    )


def test_validator_keeps_resource_types_extensible_across_providers():
    gcp = CrossCloudAlertValidator().validate(
        dict(GCP_LABELS, resource_type="future_accelerator_pool")
    )
    aws = CrossCloudAlertValidator().validate(
        dict(AWS_LABELS, resource_type="future_vector_store")
    )

    assert gcp.is_valid is True
    assert aws.is_valid is True


@pytest.mark.parametrize(
    "scope_id",
    ["12345678901", "1234567890123", "12345678901a", "１２３４５６７８９０１２"],
)
def test_validator_rejects_aws_scope_ids_that_are_not_twelve_digits(scope_id: str):
    result = CrossCloudAlertValidator().validate(
        dict(AWS_LABELS, cloud_scope_id=scope_id)
    )

    assert result.is_valid is False
    assert result.errors == (
        AlertValidationError(field="cloud_scope_id", code="invalid_value"),
    )


@pytest.mark.parametrize(
    "scope_id",
    ["a23456", "a" * 30],
)
def test_validator_accepts_gcp_scope_ids_at_the_exact_length_boundaries(scope_id: str):
    result = CrossCloudAlertValidator().validate(
        dict(
            GCP_LABELS,
            cloud_scope_id=scope_id,
            resource_id=f"projects/{scope_id}/zones/asia-east1-a/instances/vm-1",
        )
    )

    assert result.is_valid is True
    assert result.errors == ()


@pytest.mark.parametrize(
    "scope_id",
    [
        "abc12-",
        "a2345-",
        "A23456",
        "1abcde",
        "a2345_",
        "abcde",
        "a" * 31,
    ],
)
def test_validator_rejects_gcp_scope_ids_outside_the_project_id_format(scope_id: str):
    result = CrossCloudAlertValidator().validate(
        dict(GCP_LABELS, cloud_scope_id=scope_id)
    )

    assert result.is_valid is False
    assert result.errors == (
        AlertValidationError(field="cloud_scope_id", code="invalid_value"),
    )


def test_validator_reports_errors_in_required_label_order_without_raw_values():
    result = CrossCloudAlertValidator().validate(
        {"cloud_scope_id": "", "service": "", "severity": "urgent"}
    )

    assert result.is_valid is False
    assert result.errors == (
        AlertValidationError(field="alertname", code="required"),
        AlertValidationError(field="cloud_provider", code="required"),
        AlertValidationError(field="cloud_scope_id", code="required"),
        AlertValidationError(field="resource_type", code="required"),
        AlertValidationError(field="resource_id", code="required"),
        AlertValidationError(field="environment", code="required"),
        AlertValidationError(field="service", code="required"),
        AlertValidationError(field="team", code="required"),
        AlertValidationError(field="severity", code="invalid_value"),
        AlertValidationError(field="signal_type", code="required"),
    )
    assert all(not hasattr(error, "raw_payload") for error in result.errors)
    assert all(not hasattr(error, "secret") for error in result.errors)


def test_validation_errors_are_immutable_field_and_code_records():
    error = AlertValidationError(field="resource_id", code="required")

    with pytest.raises(FrozenInstanceError):
        error.code = "invalid_value"  # type: ignore[misc]


def test_incident_identity_is_canonical_and_ignores_label_input_order():
    identity = make_incident_identity(SOURCE_ONE, GCP_LABELS)
    reordered = make_incident_identity(SOURCE_ONE, dict(reversed(GCP_LABELS.items())))

    assert (
        identity == "6ad1e874633e960f554aec2bb32a092f04eaa3e63c1870d047709c527b275c98"
    )
    assert reordered == identity


@pytest.mark.parametrize(
    ("source_id", "labels"),
    [
        (SOURCE_TWO, GCP_LABELS),
        (SOURCE_ONE, AWS_LABELS),
        (
            SOURCE_ONE,
            dict(
                GCP_LABELS,
                cloud_scope_id="another-project",
                resource_id="projects/another-project/zones/asia-east1-a/instances/vm-1",
            ),
        ),
        (SOURCE_ONE, dict(GCP_LABELS, resource_type="gce_instance")),
        (
            SOURCE_ONE,
            dict(
                GCP_LABELS,
                resource_id="projects/svc-lx-afa-01-uat-1b9a87/zones/asia-east1-a/instances/vm-1",
            ),
        ),
        (SOURCE_ONE, dict(GCP_LABELS, alertname="GkePodCrashLoop")),
    ],
    ids=["source", "provider", "scope", "resource-type", "resource-id", "alertname"],
)
def test_incident_identity_changes_for_each_canonical_component(
    source_id: UUID, labels: dict[str, str]
):
    identity = make_incident_identity(source_id, labels)

    assert (
        identity != "6ad1e874633e960f554aec2bb32a092f04eaa3e63c1870d047709c527b275c98"
    )


def test_incident_identity_ignores_noncanonical_labels_and_annotations():
    identity = make_incident_identity(SOURCE_ONE, GCP_LABELS)
    labels = dict(
        GCP_LABELS,
        severity="critical",
        team="other-team",
        service="another-service",
        annotation_summary="different wording",
        groupKey="different-grouping",
    )

    assert make_incident_identity(SOURCE_ONE, labels) == identity


def test_incident_identity_rejects_invalid_labels_without_guessing_an_identity():
    labels = dict(GCP_LABELS)
    labels.pop("resource_id")

    with pytest.raises(ValueError, match="invalid cross-cloud alert labels"):
        make_incident_identity(SOURCE_ONE, labels)
