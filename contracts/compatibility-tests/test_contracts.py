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


ROOT = Path(__file__).parents[2]
CONTRACT_PATH = ROOT / "contracts" / "openapi" / "grafana-webhook-v1.yaml"


def _contract() -> dict:
    return yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))


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
