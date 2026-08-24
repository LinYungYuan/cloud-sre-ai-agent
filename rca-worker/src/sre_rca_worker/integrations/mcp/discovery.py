from __future__ import annotations

from collections.abc import Mapping

from sre_rca_worker.integrations.mcp.capability_resolver import CapabilityResolver
from sre_rca_worker.integrations.mcp.client import McpClient
from sre_rca_worker.integrations.mcp.factories import McpClientFactory
from sre_rca_worker.integrations.mcp.models import (
    CapabilitySet,
    CloudScope,
    ManifestEntry,
    SpecialistKind,
)


async def discover_capabilities(
    factory: McpClientFactory,
    scope: CloudScope | None,
    manifest: tuple[ManifestEntry, ...],
    required_by_specialist: Mapping[SpecialistKind, tuple[str, ...]],
) -> tuple[CapabilitySet, dict[SpecialistKind, McpClient]]:
    clients: dict[SpecialistKind, McpClient] = {}
    allowed = {}
    resolver = CapabilityResolver()
    for kind in SpecialistKind:
        client = factory.for_specialist(kind, scope)
        clients[kind] = client
        entries = tuple(
            item for item in manifest if item.endpoint_identity == kind.value
        )
        required = required_by_specialist.get(kind, ())
        if not required:
            allowed[kind] = ()
            continue
        discovered = await client.list_tools()
        allowed[kind] = resolver.resolve(
            required=required,
            manifest=entries,
            discovered=discovered,
            endpoint_identity=kind.value,
        )
    return CapabilitySet(by_specialist=allowed), clients
