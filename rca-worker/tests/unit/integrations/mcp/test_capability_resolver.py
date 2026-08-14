import pytest
from jsonschema import ValidationError as JsonSchemaValidationError
from pydantic import ValidationError

from sre_rca_worker.integrations.mcp.capability_resolver import CapabilityResolver
from sre_rca_worker.integrations.mcp.models import (
    DiscoveredTool,
    ManifestEntry,
    SpecialistKind,
)


def _manifest(**overrides: object) -> ManifestEntry:
    values: dict[str, object] = {
        "endpoint_identity": "metrics",
        "capability": "metrics.query",
        "tool_name_pattern": r"^metrics_query$",
        "input_schema": {
            "type": "object",
            "properties": {"project_id": {"type": "string"}},
            "required": ["project_id"],
            "additionalProperties": False,
        },
        "risk": "READ_ONLY",
    }
    values.update(overrides)
    return ManifestEntry.model_validate(values)


def _tool(**overrides: object) -> DiscoveredTool:
    values: dict[str, object] = {
        "name": "metrics_query",
        "input_schema": _manifest().input_schema,
        "annotations": {"readOnlyHint": True},
    }
    values.update(overrides)
    return DiscoveredTool.model_validate(values)


def test_resolver_exposes_only_exact_endpoint_read_only_schema_match() -> None:
    allowed = CapabilityResolver().resolve(
        required=("metrics.query",),
        manifest=(_manifest(),),
        discovered=(_tool(),),
        endpoint_identity="metrics",
    )

    assert [(item.name, item.capability) for item in allowed] == [
        ("metrics_query", "metrics.query")
    ]


def test_resolver_accepts_standard_safe_annotations_but_rejects_destructive() -> None:
    safe = _tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
    )
    assert CapabilityResolver().resolve(
        required=("metrics.query",),
        manifest=(_manifest(),),
        discovered=(safe,),
        endpoint_identity="metrics",
    )
    destructive = safe.model_copy(
        update={"annotations": safe.annotations | {"destructiveHint": True}}
    )
    assert (
        CapabilityResolver().resolve(
            required=("metrics.query",),
            manifest=(_manifest(),),
            discovered=(destructive,),
            endpoint_identity="metrics",
        )
        == ()
    )


@pytest.mark.parametrize(
    ("manifest", "tool", "endpoint"),
    [
        ((_manifest(capability="metrics.ambiguous"),), (_tool(),), "metrics"),
        ((_manifest(), _manifest()), (_tool(),), "metrics"),
        ((_manifest(),), (_tool(annotations={"readOnlyHint": False}),), "metrics"),
        ((_manifest(),), (_tool(annotations={}),), "metrics"),
        ((_manifest(),), (_tool(name="logs_query"),), "metrics"),
        ((_manifest(),), (_tool(),), "trace"),
        (
            (_manifest(),),
            (_tool(input_schema={"type": "object", "properties": {}}),),
            "metrics",
        ),
    ],
)
def test_resolver_fails_closed_for_untrusted_discovery(
    manifest: tuple[ManifestEntry, ...],
    tool: tuple[DiscoveredTool, ...],
    endpoint: str,
) -> None:
    assert (
        CapabilityResolver().resolve(
            required=("metrics.query",),
            manifest=manifest,
            discovered=tool,
            endpoint_identity=endpoint,
        )
        == ()
    )


def test_manifest_rejects_mutation_risk() -> None:
    with pytest.raises(ValidationError):
        _manifest(risk="MUTATION")


def test_arguments_are_schema_validated_before_network() -> None:
    tool = CapabilityResolver().resolve(
        required=("metrics.query",),
        manifest=(_manifest(),),
        discovered=(_tool(),),
        endpoint_identity="metrics",
    )[0]

    with pytest.raises(JsonSchemaValidationError):
        tool.validate_arguments({"project_id": "x", "endpoint": "https://evil.test"})


def test_specialist_kind_is_closed() -> None:
    assert set(SpecialistKind) == {
        SpecialistKind.METRICS,
        SpecialistKind.TRACE,
        SpecialistKind.LOG,
    }
