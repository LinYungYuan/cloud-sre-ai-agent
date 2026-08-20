from dataclasses import dataclass
from typing import Literal

CanonicalSeverity = Literal["SEV1", "SEV3", "UNMAPPED"]


@dataclass(frozen=True, slots=True)
class SeverityDecision:
    raw: str | None
    canonical: CanonicalSeverity
    warnings: tuple[str, ...]


def map_severity(raw: object) -> SeverityDecision:
    raw_string = raw if isinstance(raw, str) else None
    normalized = raw_string.strip().upper() if raw_string is not None else ""
    if normalized == "ERROR":
        return SeverityDecision(raw=raw_string, canonical="SEV1", warnings=())
    if normalized in {"WARN", "WARNING"}:
        return SeverityDecision(raw=raw_string, canonical="SEV3", warnings=())
    return SeverityDecision(
        raw=raw_string,
        canonical="UNMAPPED",
        warnings=("severity_unmapped",),
    )
