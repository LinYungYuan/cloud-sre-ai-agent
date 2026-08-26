from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import datetime

from sre_rca_worker.integrations.mcp.capability_resolver import CapabilityResolver
from sre_rca_worker.integrations.mcp.client import McpClient
from sre_rca_worker.integrations.mcp.factories import McpClientFactory
from sre_rca_worker.integrations.mcp.models import (
    AllowedTool,
    CapabilitySet,
    CloudScope,
    DiscoveryFailure,
    ManifestEntry,
    SpecialistKind,
)

_ORDER = (SpecialistKind.METRICS, SpecialistKind.TRACE, SpecialistKind.LOG)


async def discover_capabilities(
    factory: McpClientFactory,
    scope: CloudScope | None,
    manifest: tuple[ManifestEntry, ...],
    required_by_specialist: Mapping[SpecialistKind, tuple[str, ...]],
    *,
    deadline: datetime | None = None,
) -> tuple[CapabilitySet, dict[SpecialistKind, McpClient]]:
    if deadline is not None and (
        deadline.tzinfo is None or deadline.utcoffset() is None
    ):
        raise ValueError("discovery deadline must be timezone-aware")
    if scope is None or scope.provider != "GCP" or not scope.safe:
        return CapabilitySet(by_specialist={}), {}

    resolver = CapabilityResolver()
    candidates: list[
        tuple[SpecialistKind, tuple[ManifestEntry, ...], tuple[str, ...]]
    ] = []
    for kind in _ORDER:
        entries = tuple(
            item for item in manifest if item.endpoint_identity == kind.value
        )
        required = required_by_specialist.get(kind, ())
        if not required or any(
            sum(entry.capability == capability for entry in entries) != 1
            for capability in required
        ):
            continue
        candidates.append((kind, entries, required))

    async def discover_one(
        kind: SpecialistKind,
        entries: tuple[ManifestEntry, ...],
        required: tuple[str, ...],
    ) -> tuple[
        SpecialistKind,
        McpClient | None,
        tuple[AllowedTool, ...],
        DiscoveryFailure | None,
    ]:
        if deadline is not None:
            remaining = (deadline - datetime.now(deadline.tzinfo)).total_seconds()
            if remaining <= 0:
                return (
                    kind,
                    None,
                    (),
                    DiscoveryFailure(specialist=kind, code="MCP_TIMEOUT"),
                )
        try:
            client = factory.for_specialist(kind, scope)
            if deadline is None:
                discovered = await client.list_tools()
            else:
                remaining = (deadline - datetime.now(deadline.tzinfo)).total_seconds()
                if remaining <= 0:
                    return (
                        kind,
                        client,
                        (),
                        DiscoveryFailure(specialist=kind, code="MCP_TIMEOUT"),
                    )
                async with asyncio.timeout(remaining):
                    discovered = await client.list_tools()
            if not isinstance(discovered, tuple):
                raise TypeError("MCP discovery result must be a tuple")
            return (
                kind,
                client,
                resolver.resolve(
                    required=required,
                    manifest=entries,
                    discovered=discovered,
                    endpoint_identity=kind.value,
                ),
                None,
            )
        except TimeoutError:
            return kind, None, (), DiscoveryFailure(specialist=kind, code="MCP_TIMEOUT")
        except (ConnectionError, OSError):
            return (
                kind,
                None,
                (),
                DiscoveryFailure(specialist=kind, code="MCP_TRANSPORT"),
            )
        except Exception:  # noqa: BLE001 - discovery is a safe per-endpoint boundary
            return (
                kind,
                None,
                (),
                DiscoveryFailure(specialist=kind, code="MCP_RESULT_INVALID"),
            )

    discovered_results = await asyncio.gather(
        *(
            discover_one(kind, entries, required)
            for kind, entries, required in candidates
        )
    )
    allowed = {kind: tools for kind, _client, tools, _failure in discovered_results}
    clients = {
        kind: client
        for kind, client, _tools, failure in discovered_results
        if client is not None and failure is None
    }
    failures = tuple(
        failure
        for kind in _ORDER
        for candidate_kind, _client, _tools, failure in discovered_results
        if candidate_kind is kind and failure is not None
    )
    return CapabilitySet(by_specialist=allowed, discovery_failures=failures), clients
