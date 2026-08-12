import json
import shutil
import sys
from importlib import import_module
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

sys.path.insert(0, str(Path(__file__).parents[2]))

validate_all = import_module("scripts.contract_check.check_contracts").validate_all


ROOT = Path(__file__).parents[2]
CONTRACT_PATH = ROOT / "contracts" / "openapi" / "grafana-webhook-v1.yaml"
OPERATOR_CONTRACT_PATH = ROOT / "contracts" / "openapi" / "operator-api-v1.yaml"
INCIDENT_EVENTS_PATH = ROOT / "contracts" / "events" / "incident-events-v1.json"


def _contract() -> dict:
    return yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))


def _operator_contract() -> dict:
    return yaml.safe_load(OPERATOR_CONTRACT_PATH.read_text(encoding="utf-8"))


def _incident_event_contract() -> dict:
    return json.loads(INCIDENT_EVENTS_PATH.read_text(encoding="utf-8"))


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
    alert = schemas["GrafanaAlert"]
    assert set(alert["properties"]) == expected_alert_fields
    assert set(alert["required"]) == expected_alert_fields
    assert schemas["GrafanaWebhook"]["additionalProperties"] is True
    assert alert["additionalProperties"] is True
    assert schemas["WebhookAccepted"]["properties"]["acceptedAt"]["pattern"] == "Z$"


@pytest.mark.parametrize(
    ("example_name", "path", "invalid_value"),
    [
        ("grafana-firing.json", ("alerts", 0, "startsAt"), "not-a-timestamp"),
        ("grafana-firing.json", ("alerts", 0, "generatorURL"), "not-a-uri"),
        ("webhook-accepted.json", ("eventId",), "not-a-uuid"),
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
        ("/api/v1/events/stream", "get"),
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
            for status, response_reference in operation["responses"].items():
                if int(status) < 400:
                    continue
                response = _resolve_local_ref(contract, response_reference)
                assert set(response["content"]) == {"application/problem+json"}


def test_sse_contract_is_replayable_and_has_no_arbitrary_payload():
    operator_contract = _operator_contract()
    stream = operator_contract["paths"]["/api/v1/events/stream"]["get"]
    parameters = {
        _resolve_local_ref(operator_contract, parameter)["name"]: _resolve_local_ref(
            operator_contract, parameter
        )
        for parameter in stream["parameters"]
    }
    assert parameters["after"]["in"] == "query"
    assert parameters["Last-Event-ID"]["in"] == "header"
    assert set(stream["responses"]["200"]["content"]) == {"text/event-stream"}

    event_schema = _incident_event_contract()
    assert set(event_schema["required"]) == {
        "eventId",
        "type",
        "incidentId",
        "resourceId",
        "occurredAt",
        "version",
    }
    assert event_schema["properties"]["version"] == {"type": "integer", "const": 1}
    assert event_schema["additionalProperties"] is False
    assert "payload" not in event_schema["properties"]
    assert "raw" not in event_schema["properties"]
    assert set(event_schema["properties"]["type"]["enum"]) == {
        "INCIDENT_CREATED",
        "INCIDENT_ACKNOWLEDGED",
        "INCIDENT_ASSIGNED",
        "INCIDENT_RESOLVED",
        "INCIDENT_REOPENED",
        "ALERT_FIRING",
        "ALERT_RESOLVED",
        "ALERT_CLASSIFIED",
        "RCA_RUN_CREATED",
        "RCA_RUN_STATUS_CHANGED",
        "RCA_REPORT_PUBLISHED",
        "MESSAGE_CREATED",
    }

    valid_event = {
        "eventId": "9ad048da-37d2-4fc6-8ef8-dc17ddb7af80",
        "type": "RCA_REPORT_PUBLISHED",
        "incidentId": "e276fdf9-8600-41f7-8c32-fd3cbed55247",
        "resourceId": "aab5602d-a58f-4617-b4ad-81233ce38125",
        "occurredAt": "2026-08-12T02:45:00Z",
        "version": 1,
    }
    Draft202012Validator(
        event_schema, format_checker=Draft202012Validator.FORMAT_CHECKER
    ).validate(valid_event)
    with pytest.raises(ValidationError):
        Draft202012Validator(event_schema).validate(
            {**valid_event, "payload": {"rawEvidence": "must not cross SSE"}}
        )
