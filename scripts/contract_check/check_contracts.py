"""Validate versioned API contracts and their shipped example payloads."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from openapi_spec_validator import validate
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as contract_file:
        return yaml.safe_load(contract_file)


def _load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as example_file:
        return json.load(example_file)


def _validate_example(
    example: dict[str, Any], schema_name: str, contract: dict[str, Any], contract_path: Path
) -> None:
    resolver = Registry().with_resource(
        contract_path.as_uri(), Resource.from_contents(contract, default_specification=DRAFT202012)
    )
    schema_reference = {"$ref": f"{contract_path.as_uri()}#/components/schemas/{schema_name}"}
    Draft202012Validator(schema_reference, registry=resolver).validate(example)


def validate_all(root: Path) -> None:
    """Validate all OpenAPI documents, component schemas, and example payloads."""
    contracts_root = root / "contracts"

    for contract_path in sorted((contracts_root / "openapi").glob("*.yaml")):
        contract = _load_yaml(contract_path)
        validate(contract, base_uri=contract_path.as_uri())

        schemas = contract["components"]["schemas"]
        for schema in schemas.values():
            Draft202012Validator.check_schema(schema)

        if contract_path.name == "grafana-webhook-v1.yaml":
            _validate_example(
                _load_json(contracts_root / "examples" / "grafana-firing.json"),
                "GrafanaWebhook",
                contract,
                contract_path,
            )
            _validate_example(
                _load_json(contracts_root / "examples" / "grafana-resolved.json"),
                "GrafanaWebhook",
                contract,
                contract_path,
            )
            _validate_example(
                _load_json(contracts_root / "examples" / "webhook-accepted.json"),
                "WebhookAccepted",
                contract,
                contract_path,
            )
