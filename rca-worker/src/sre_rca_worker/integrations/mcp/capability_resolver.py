import re

from sre_rca_worker.integrations.mcp.models import (
    AllowedTool,
    DiscoveredTool,
    ManifestEntry,
    schema_hash,
)


class CapabilityResolver:
    """Resolve discovery results against a closed, endpoint-bound manifest."""

    def resolve(
        self,
        *,
        required: tuple[str, ...],
        manifest: tuple[ManifestEntry, ...],
        discovered: tuple[DiscoveredTool, ...],
        endpoint_identity: str,
    ) -> tuple[AllowedTool, ...]:
        resolved: list[AllowedTool] = []
        for capability in required:
            entries = [
                entry
                for entry in manifest
                if entry.capability == capability
                and entry.endpoint_identity == endpoint_identity
            ]
            if len(entries) != 1:
                return ()
            entry = entries[0]
            matches = [
                tool
                for tool in discovered
                if re.fullmatch(entry.tool_name_pattern, tool.name)
                and tool.annotations.get("readOnlyHint") is True
                and set(tool.annotations) <= {"readOnlyHint"}
                and schema_hash(tool.input_schema) == entry.input_schema_hash
            ]
            if len(matches) != 1:
                return ()
            resolved.append(
                AllowedTool(
                    name=matches[0].name,
                    capability=capability,
                    endpoint_identity=entry.endpoint_identity,
                    input_schema=entry.input_schema,
                )
            )
        return tuple(resolved)
