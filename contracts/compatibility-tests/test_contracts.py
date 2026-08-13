import json
import shutil
import sys
from importlib import import_module
from pathlib import Path

import pytest
import yaml
from jsonschema.exceptions import ValidationError

sys.path.insert(0, str(Path(__file__).parents[2]))

validate_all = import_module("scripts.contract_check.check_contracts").validate_all
validate_example = import_module(
    "scripts.contract_check.check_contracts"
)._validate_example


ROOT = Path(__file__).parents[2]
CONTRACT_PATH = ROOT / "contracts" / "openapi" / "grafana-webhook-v1.yaml"
OPERATOR_CONTRACT_PATH = ROOT / "contracts" / "openapi" / "operator-api-v1.yaml"
TABLE_OWNERSHIP_PATH = ROOT / "contracts" / "database" / "table-ownership.yaml"
LOCAL_COMPOSE_PATH = ROOT / "docker-compose.yml"


def _contract() -> dict:
    return yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))


def _operator_contract() -> dict:
    return yaml.safe_load(OPERATOR_CONTRACT_PATH.read_text(encoding="utf-8"))


def _local_compose() -> dict:
    return yaml.safe_load(LOCAL_COMPOSE_PATH.read_text(encoding="utf-8"))


def _resolve_local_ref(document: dict, value: dict) -> dict:
    if "$ref" not in value:
        return value
    resolved = document
    for part in value["$ref"].removeprefix("#/").split("/"):
        resolved = resolved[part]
    return resolved


def _copy_contracts(tmp_path: Path) -> Path:
    shutil.copytree(ROOT / "contracts", tmp_path / "contracts")
    return tmp_path


def _write_example(root: Path, name: str, payload: dict) -> None:
    (root / "contracts" / "examples" / name).write_text(
        json.dumps(payload), encoding="utf-8"
    )


def test_all_contracts_and_examples_are_valid():
    validate_all(ROOT)


def test_local_postgres_compose_restricts_passwordless_access_to_loopback():
    """Protect local-only trust authentication from becoming network reachable."""
    postgres = _local_compose()["services"]["postgres"]
    environment = postgres["environment"]
    password_key = "POSTGRES" + "_PASSWORD"

    assert environment["POSTGRES_HOST_AUTH_METHOD"] == "trust"
    assert password_key not in environment
    assert postgres["ports"] == ["127.0.0.1:55432:5432"]


@pytest.mark.parametrize("example_name", ["grafana-firing.json", "grafana-firing-aws.json"])
def test_cross_cloud_grafana_fixtures_use_the_standard_v1_envelope(
    example_name: str,
) -> None:
    contract = _contract()
    payload = json.loads((ROOT / "contracts" / "examples" / example_name).read_text())

    assert {
        "receiver",
        "status",
        "orgId",
        "alerts",
        "groupLabels",
        "commonLabels",
        "commonAnnotations",
        "externalURL",
        "version",
        "groupKey",
        "truncatedAlerts",
        "title",
        "message",
    } <= set(payload)
    assert payload["version"] == "1"
    validate_example(
        payload,
        "GrafanaWebhook",
        contract,
        CONTRACT_PATH,
    )


def test_provider_examples_use_only_project_id_presence() -> None:
    aws = json.loads(
        (ROOT / "contracts" / "examples" / "grafana-firing-aws.json").read_text()
    )
    gcp = json.loads(
        (ROOT / "contracts" / "examples" / "grafana-firing.json").read_text()
    )

    aws_labels = aws["alerts"][0]["labels"]
    gcp_labels = gcp["alerts"][0]["labels"]
    assert "resource.label.project_id" not in aws_labels
    assert gcp_labels["resource.label.project_id"].strip()
    for labels in (aws_labels, gcp_labels):
        assert "cloud_provider" not in labels
        assert "team" not in labels
        assert "environment" not in labels
        assert "service" not in labels
        assert labels["folder"] == "COM-LX-BOA-01"
        assert labels["alertname"] == "High CPU usage"
        assert labels["severity"] == "ERROR"
        assert labels["DBInstanceIdentifier"] == "production-rds-01"
        assert labels["Series"] == "123456789012"
        assert (
            aws["alerts"][0]["annotations"]["AlertValues"]
            == gcp["alerts"][0]["annotations"]["AlertValues"]
        )


def test_grafana_webhook_contract_locks_platform_boundary():
    operation = _contract()["paths"]["/webhooks/v1/grafana/{sourceId}"]["post"]
    schemas = _contract()["components"]["schemas"]
    assert schemas["GrafanaWebhook"]["properties"]["alerts"]["minItems"] == 1

    assert operation["security"] == [{"bearerAuth": []}]
    assert "1 MiB" in operation["description"]
    for status in ("400", "401", "413", "500"):
        assert set(operation["responses"][status]["content"]) == {
            "application/problem+json"
        }

    expected_alert_fields = {
        "status",
        "labels",
        "annotations",
        "startsAt",
        "endsAt",
        "values",
        "generatorURL",
        "fingerprint",
        "silenceURL",
        "dashboardURL",
        "panelURL",
        "imageURL",
    }
    required_alert_fields = {
        "status",
        "labels",
        "annotations",
        "startsAt",
        "endsAt",
        "values",
        "generatorURL",
        "fingerprint",
    }
    alert = schemas["GrafanaAlert"]
    assert set(alert["properties"]) == expected_alert_fields
    assert set(alert["required"]) == required_alert_fields
    assert schemas["GrafanaWebhook"]["additionalProperties"] is True
    assert alert["additionalProperties"] is True

    accepted = schemas["WebhookAccepted"]
    assert set(accepted["required"]) == {"deliveryId", "acceptedAt"}
    assert set(accepted["properties"]) == {"deliveryId", "acceptedAt"}
    assert accepted["properties"]["deliveryId"]["format"] == "uuid"
    assert accepted["properties"]["acceptedAt"]["pattern"] == "Z$"
    assert accepted["additionalProperties"] is False


def test_validator_accepts_official_default_grafana_link_semantics(tmp_path: Path):
    root = _copy_contracts(tmp_path)
    example_path = root / "contracts" / "examples" / "grafana-firing.json"
    payload = json.loads(example_path.read_text(encoding="utf-8"))
    alert = payload["alerts"][0]
    alert["dashboardURL"] = ""
    alert["panelURL"] = ""
    alert.pop("imageURL", None)
    _write_example(root, "grafana-firing.json", payload)

    validate_all(root)


@pytest.mark.parametrize(
    ("example_name", "path", "invalid_value"),
    [
        ("grafana-firing.json", ("alerts", 0, "startsAt"), "not-a-timestamp"),
        ("grafana-firing.json", ("alerts", 0, "generatorURL"), "not-a-uri"),
        ("webhook-accepted.json", ("deliveryId",), "not-a-uuid"),
    ],
)
def test_validator_rejects_invalid_date_time_uri_and_uuid_formats(
    tmp_path: Path, example_name: str, path: tuple[str | int, ...], invalid_value: str
):
    root = _copy_contracts(tmp_path)
    example_path = root / "contracts" / "examples" / example_name
    payload = json.loads(example_path.read_text(encoding="utf-8"))
    target = payload
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = invalid_value
    _write_example(root, example_name, payload)

    with pytest.raises(ValidationError):
        validate_all(root)


def test_validator_preserves_unknown_grafana_fields(tmp_path: Path):
    root = _copy_contracts(tmp_path)
    example_path = root / "contracts" / "examples" / "grafana-firing.json"
    payload = json.loads(example_path.read_text(encoding="utf-8"))
    payload["futureGrafanaTopLevelField"] = "preserved"
    payload["alerts"][0]["futureGrafanaAlertField"] = "preserved"
    _write_example(root, "grafana-firing.json", payload)

    validate_all(root)


@pytest.mark.parametrize(
    ("example_name", "path", "invalid_value"),
    [
        ("incident.json", ("openedAt",), "2026-08-12T10:30:00+08:00"),
        ("rca-report.json", ("incidentId",), "not-a-uuid"),
    ],
)
def test_validator_rejects_non_z_timestamps_and_invalid_operator_uuids(
    tmp_path: Path, example_name: str, path: tuple[str | int, ...], invalid_value: str
):
    root = _copy_contracts(tmp_path)
    example_path = root / "contracts" / "examples" / example_name
    payload = json.loads(example_path.read_text(encoding="utf-8"))
    target = payload
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = invalid_value
    _write_example(root, example_name, payload)

    with pytest.raises(ValidationError):
        validate_all(root)


def test_operator_contract_declares_every_approved_operation():
    paths = _operator_contract()["paths"]
    expected_operations = {
        ("/api/v1/dashboard/summary", "get"),
        ("/api/v1/incidents", "get"),
        ("/api/v1/incidents/{id}", "get"),
        ("/api/v1/incidents/{id}/timeline", "get"),
        ("/api/v1/incidents/{id}/acknowledge", "post"),
        ("/api/v1/incidents/{id}/assign", "post"),
        ("/api/v1/incidents/{id}/resolve", "post"),
        ("/api/v1/incidents/{id}/reopen", "post"),
        ("/api/v1/alerts", "get"),
        ("/api/v1/alerts/{id}", "get"),
        ("/api/v1/unclassified-alerts", "get"),
        ("/api/v1/unclassified-alerts/{id}/classify", "post"),
        ("/api/v1/classification-mappings", "get"),
        ("/api/v1/classification-mappings", "post"),
        ("/api/v1/classification-mappings/{id}", "patch"),
        ("/api/v1/classification-mappings/{id}", "delete"),
        ("/api/v1/incidents/{id}/rca-runs", "get"),
        ("/api/v1/incidents/{id}/rca-runs", "post"),
        ("/api/v1/rca-runs/{id}", "get"),
        ("/api/v1/rca-runs/{id}/report", "get"),
        ("/api/v1/rca-runs/{id}/evidence", "get"),
        ("/api/v1/rca-runs/{id}/hypotheses", "get"),
    }
    actual_operations = {
        (path, method)
        for path, path_item in paths.items()
        for method in path_item
        if method in {"get", "post", "patch", "delete"}
    }

    assert actual_operations == expected_operations


def test_operator_mutations_declare_a_concurrency_or_idempotency_header():
    contract = _operator_contract()
    for path, path_item in contract["paths"].items():
        for method in ("post", "patch", "delete"):
            if operation := path_item.get(method):
                parameter_names = {
                    _resolve_local_ref(contract, parameter)["name"]
                    for parameter in operation.get("parameters", [])
                }
                assert parameter_names & {"Idempotency-Key", "If-Match"}, (
                    f"{method.upper()} {path} lacks a mutation-safety header"
                )
                safety_parameters = [
                    _resolve_local_ref(contract, parameter)
                    for parameter in operation.get("parameters", [])
                    if _resolve_local_ref(contract, parameter)["name"]
                    in {"Idempotency-Key", "If-Match"}
                ]
                assert all(
                    parameter["in"] == "header" for parameter in safety_parameters
                )
                assert all(
                    parameter["required"] is True for parameter in safety_parameters
                )


def test_operator_collection_schemas_are_cursor_pages_without_offsets():
    contract = _operator_contract()
    schemas = contract["components"]["schemas"]
    cursor_pages = {
        name: schema
        for name, schema in schemas.items()
        if name.startswith("CursorPage")
    }

    assert cursor_pages
    for name, schema in cursor_pages.items():
        assert set(schema["required"]) == {"items", "nextCursor"}, name
        assert set(schema["properties"]) == {"items", "nextCursor"}, name
        assert "offset" not in schema["properties"], name

    expected_page_by_path = {
        "/api/v1/incidents": "CursorPageIncidents",
        "/api/v1/incidents/{id}/timeline": "CursorPageTimelineEvents",
        "/api/v1/alerts": "CursorPageAlerts",
        "/api/v1/unclassified-alerts": "CursorPageAlerts",
        "/api/v1/classification-mappings": "CursorPageClassificationMappings",
        "/api/v1/incidents/{id}/rca-runs": "CursorPageRcaRuns",
        "/api/v1/rca-runs/{id}/evidence": "CursorPageEvidence",
        "/api/v1/rca-runs/{id}/hypotheses": "CursorPageHypotheses",
    }
    for path, page_schema in expected_page_by_path.items():
        operation = contract["paths"][path]["get"]
        response_schema = operation["responses"]["200"]["content"]["application/json"][
            "schema"
        ]
        assert response_schema == {"$ref": f"#/components/schemas/{page_schema}"}, path
        assert "offset" not in {
            _resolve_local_ref(contract, parameter)["name"]
            for parameter in operation.get("parameters", [])
        }, path


def test_operator_contract_locks_public_enums_errors_and_incident_etags():
    contract = _operator_contract()
    schemas = contract["components"]["schemas"]

    assert schemas["AlertState"]["enum"] == ["FIRING", "RESOLVED"]
    assert schemas["ClassificationStatus"]["enum"] == [
        "CLASSIFIED",
        "UNCLASSIFIED",
    ]
    assert schemas["IncidentStatus"]["enum"] == [
        "OPEN",
        "INVESTIGATING",
        "RESOLVED",
    ]
    assert schemas["RcaRunStatus"]["enum"] == [
        "WAITING_FOR_CLASSIFICATION",
        "QUEUED",
        "RUNNING",
        "SUCCEEDED",
        "PARTIAL",
        "FAILED",
        "CANCELLED",
    ]
    assert {
        "RCA_ALREADY_RUNNING",
        "INCIDENT_VERSION_CONFLICT",
        "SCOPE_FORBIDDEN",
        "MCP_TIMEOUT",
    } <= set(schemas["ProblemCode"]["enum"])
    assert set(schemas["Problem"]["required"]) == {
        "type",
        "title",
        "status",
        "code",
        "correlationId",
    }

    incident_operations = [
        contract["paths"]["/api/v1/incidents/{id}"]["get"],
        *(
            contract["paths"][f"/api/v1/incidents/{{id}}/{action}"]["post"]
            for action in ("acknowledge", "assign", "resolve", "reopen")
        ),
    ]
    for operation in incident_operations:
        success = _resolve_local_ref(contract, operation["responses"]["200"])
        assert "ETag" in success["headers"]


def test_operator_errors_are_problem_json_and_timestamps_are_utc_z():
    contract = _operator_contract()
    assert contract["components"]["schemas"]["UtcDateTime"] == {
        "type": "string",
        "format": "date-time",
        "pattern": "Z$",
        "description": "UTC timestamp serialized in RFC 3339 Z notation.",
    }

    for path_item in contract["paths"].values():
        for method, operation in path_item.items():
            if method not in {"get", "post", "patch", "delete"}:
                continue
            assert {"400", "401", "403", "404", "409", "500"} <= set(
                operation["responses"]
            )
            for status, response_reference in operation["responses"].items():
                if int(status) < 400:
                    continue
                response = _resolve_local_ref(contract, response_reference)
                assert set(response["content"]) == {"application/problem+json"}


def test_operator_boundary_is_rest_only():
    assert "/api/v1/events/stream" not in _operator_contract()["paths"]
    assert "/api/v1/incidents/{id}/messages" not in _operator_contract()["paths"]
    assert not (ROOT / "contracts" / "events" / "incident-events-v1.json").exists()


def test_operator_alert_exposes_normalized_issue_and_nullable_scope() -> None:
    schemas = _operator_contract()["components"]["schemas"]
    alert = schemas["AlertDetail"]

    assert {
        "provider",
        "folderCode",
        "alertName",
        "severityRaw",
        "severity",
        "issue",
        "normalizationWarnings",
    } <= set(alert["required"])
    assert schemas["Provider"]["enum"] == ["GCP", "AWS"]
    assert schemas["CanonicalSeverity"]["enum"] == ["SEV1", "SEV3", "UNMAPPED"]
    assert schemas["AlertIssue"]["required"] == [
        "rawText",
        "source",
        "contentType",
        "untrusted",
    ]
    assert schemas["AlertIssue"]["properties"]["source"] == {
        "const": "grafana.annotations.AlertValues"
    }
    assert schemas["AlertIssue"]["properties"]["contentType"] == {
        "const": "text/plain"
    }
    assert schemas["AlertIssue"]["properties"]["untrusted"] == {"const": True}
    assert all(
        property_schema["type"] == ["string", "null"]
        for property_schema in schemas["Scope"]["properties"].values()
    )
    assert schemas["AlertDetail"]["properties"]["labels"] == {
        "type": "object",
        "additionalProperties": True,
    }
    assert schemas["EvidenceReference"]["required"] == [
        "evidenceId",
        "partitionTimestamp",
        "relation",
    ]


def test_every_existing_table_has_one_migration_owner() -> None:
    manifest = yaml.safe_load(TABLE_OWNERSHIP_PATH.read_text(encoding="utf-8"))
    table_entries = manifest["tables"]
    table_names = [entry["name"] for entry in table_entries]
    expected_tables = {
        "teams",
        "projects",
        "environments",
        "services",
        "subjects",
        "scope_grants",
        "grafana_sources",
        "webhook_deliveries",
        "ingestion_dedup_keys",
        "alert_events",
        "alert_instances",
        "classification_mappings",
        "incidents",
        "incident_alerts",
        "incident_assignments",
        "incident_status_history",
        "rca_runs",
        "specialist_runs",
        "evidence_records",
        "rca_hypotheses",
        "hypothesis_evidence",
        "rca_reports",
        "incident_messages",
        "incident_timeline_events",
        "audit_events",
        "outbox_events",
        "worker_jobs",
        "worker_attempts",
    }

    assert len(table_names) == len(set(table_names))
    assert set(table_names) == expected_tables
    assert all(entry["migrationOwner"] in {"backend", "rca-worker"} for entry in table_entries)
    assert manifest["databaseAccess"]["applicationRole"] == "shared"

    by_name = {entry["name"]: entry for entry in table_entries}
    worker_owned = {
        "rca_runs",
        "specialist_runs",
        "evidence_records",
        "rca_hypotheses",
        "hypothesis_evidence",
        "rca_reports",
        "worker_jobs",
        "worker_attempts",
    }
    assert all(by_name[name]["migrationOwner"] == "rca-worker" for name in worker_owned)
    assert all(by_name[name]["legacyMigrationOwner"] == "backend" for name in worker_owned)
    assert by_name["incident_messages"] == {
        "name": "incident_messages",
        "migrationOwner": "backend",
        "status": "legacy-reserved-unused",
    }

    future_backend_migrations = [
        path
        for path in (ROOT / "backend" / "migrations" / "versions").glob("*.py")
        if not path.name.startswith("0001_")
    ]
    for migration_path in future_backend_migrations:
        migration = migration_path.read_text(encoding="utf-8")
        assert not any(
            f'"{table_name}"' in migration or f"'{table_name}'" in migration
            for table_name in worker_owned
        ), f"{migration_path.name} touches an RCA Worker-owned table"


def test_dashboard_contract_covers_approved_operator_summary():
    contract = _operator_contract()
    schemas = contract["components"]["schemas"]
    dashboard = schemas["DashboardSummary"]
    operation = contract["paths"]["/api/v1/dashboard/summary"]["get"]
    parameters = {
        _resolve_local_ref(contract, parameter)["name"]: _resolve_local_ref(
            contract, parameter
        )
        for parameter in operation["parameters"]
    }

    assert {
        "openIncidents",
        "criticalIncidents",
        "unacknowledgedIncidents",
        "unassignedIncidents",
        "rcaStatusCounts",
        "unclassifiedAlerts",
        "recentIncidents",
        "alertTrend24h",
        "generatedAt",
    } <= set(dashboard["required"])
    assert dashboard["properties"]["recentIncidents"]["items"] == {
        "$ref": "#/components/schemas/IncidentSummary"
    }
    assert dashboard["properties"]["alertTrend24h"]["items"] == {
        "$ref": "#/components/schemas/AlertTrendPoint"
    }
    assert set(schemas["RcaStatusCounts"]["required"]) == {
        "queued",
        "running",
        "partial",
        "failed",
    }
    assert set(parameters) == {
        "from",
        "to",
        "teamId",
        "projectId",
        "environmentId",
    }
    for scope_filter in ("teamId", "projectId", "environmentId"):
        assert parameters[scope_filter]["in"] == "query"
        assert parameters[scope_filter]["required"] is False
        assert parameters[scope_filter]["schema"] == {
            "type": "string",
            "format": "uuid",
        }


def test_incident_list_contract_exposes_scope_filters_sorting_and_rca_status():
    contract = _operator_contract()
    incident = contract["components"]["schemas"]["IncidentSummary"]
    operation = contract["paths"]["/api/v1/incidents"]["get"]
    parameters = {
        _resolve_local_ref(contract, parameter)["name"]: _resolve_local_ref(
            contract, parameter
        )
        for parameter in operation["parameters"]
    }

    assert "rcaStatus" in incident["required"]
    assert incident["properties"]["severity"] == {
        "$ref": "#/components/schemas/CanonicalSeverity"
    }
    assert set(parameters) == {
        "cursor",
        "limit",
        "from",
        "to",
        "teamId",
        "projectId",
        "environmentId",
        "serviceId",
        "severity",
        "status",
        "rcaStatus",
        "assigneeId",
        "search",
        "sortBy",
        "sortOrder",
    }
    assert parameters["sortBy"]["schema"]["enum"] == [
        "openedAt",
        "updatedAt",
        "severity",
        "status",
    ]
    assert parameters["sortOrder"]["schema"]["enum"] == ["asc", "desc"]


def test_mapping_items_publish_etag_for_if_match_updates():
    contract = _operator_contract()
    mapping = contract["components"]["schemas"]["ClassificationMapping"]
    if_match = contract["components"]["parameters"]["IfMatch"]

    assert "etag" in mapping["required"]
    assert mapping["properties"]["etag"]["description"].startswith("Opaque")
    assert mapping["properties"]["etag"]["pattern"] == if_match["schema"]["pattern"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.pop("acknowledgedAt"),
        lambda payload: payload.update(status="RESOLVED", resolvedAt=None),
    ],
    ids=["acknowledged-without-actor-time", "resolved-without-resolved-at"],
)
def test_incident_example_rejects_impossible_lifecycle_state(tmp_path: Path, mutate):
    root = _copy_contracts(tmp_path)
    example_path = root / "contracts" / "examples" / "incident.json"
    payload = json.loads(example_path.read_text(encoding="utf-8"))
    mutate(payload)
    _write_example(root, "incident.json", payload)

    with pytest.raises(ValidationError):
        validate_all(root)
