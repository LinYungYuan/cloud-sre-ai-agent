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
validate_example = import_module("scripts.contract_check.check_contracts")._validate_example


ROOT = Path(__file__).parents[2]
CONTRACT_PATH = ROOT / "contracts" / "openapi" / "grafana-webhook-v1.yaml"
OPERATOR_CONTRACT_PATH = ROOT / "contracts" / "openapi" / "operator-api-v1.yaml"


def _contract() -> dict:
    return yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))


def _operator_contract() -> dict:
    return yaml.safe_load(OPERATOR_CONTRACT_PATH.read_text(encoding="utf-8"))


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


@pytest.mark.parametrize(
    ("example_name", "expected_provider"),
    [
        ("grafana-firing.json", "gcp"),
        ("grafana-firing-aws.json", "aws"),
    ],
)
def test_cross_cloud_grafana_fixtures_use_the_standard_v1_envelope(
    example_name: str, expected_provider: str
):
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
    assert payload["alerts"][0]["labels"]["cloud_provider"] == expected_provider
    assert payload["groupLabels"]["resource_id"] in payload["groupKey"]
    validate_example(
        payload,
        "GrafanaWebhook",
        contract,
        CONTRACT_PATH,
    )


def test_grafana_webhook_contract_locks_platform_boundary():
    operation = _contract()["paths"]["/webhooks/v1/grafana/{sourceId}"]["post"]
    schemas = _contract()["components"]["schemas"]

    assert operation["security"] == [{"bearerAuth": []}]
    assert "1 MiB" in operation["description"]
    for status in ("400", "401", "413", "500"):
        assert set(operation["responses"][status]["content"]) == {"application/problem+json"}

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
        ("/api/v1/incidents/{id}/messages", "get"),
        ("/api/v1/incidents/{id}/messages", "post"),
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
                assert all(parameter["in"] == "header" for parameter in safety_parameters)
                assert all(parameter["required"] is True for parameter in safety_parameters)


def test_operator_collection_schemas_are_cursor_pages_without_offsets():
    contract = _operator_contract()
    schemas = contract["components"]["schemas"]
    cursor_pages = {
        name: schema for name, schema in schemas.items() if name.startswith("CursorPage")
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
        "/api/v1/incidents/{id}/messages": "CursorPageIncidentMessages",
    }
    for path, page_schema in expected_page_by_path.items():
        operation = contract["paths"][path]["get"]
        response_schema = operation["responses"]["200"]["content"][
            "application/json"
        ]["schema"]
        assert response_schema == {
            "$ref": f"#/components/schemas/{page_schema}"
        }, path
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
    assert not (ROOT / "contracts" / "events" / "incident-events-v1.json").exists()


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
