import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from sre_rca_worker.agents.rca.models import IncidentContext
from sre_rca_worker.agents.rca.router import RuleRouter
from sre_rca_worker.agents.rca.synthesizer import RcaSynthesizer
from sre_rca_worker.integrations.mcp.models import (
    AllowedTool,
    CapabilitySet,
    CloudScope,
    SpecialistKind,
)

DATASETS = Path(__file__).with_name("datasets")


@pytest.mark.parametrize("path", sorted(DATASETS.glob("*.json")), ids=lambda p: p.stem)
def test_route_and_partial_report_safety_dataset(path: Path) -> None:
    case = json.loads(path.read_text())
    now = datetime(2026, 8, 13, tzinfo=UTC)
    context = IncidentContext(
        incident_id=uuid4(),
        rca_run_id=uuid4(),
        alert_issue=case["alertIssue"],
        scope=CloudScope(
            provider=case["provider"], scope_id=case["scopeId"], safe=case["safe"]
        ),
        window_start=now - timedelta(minutes=15),
        window_end=now,
    )
    capabilities = CapabilitySet(
        by_specialist={
            SpecialistKind(kind): (
                AllowedTool(
                    name=f"{kind}_query",
                    capability=f"{kind}.query",
                    endpoint_identity=kind,
                    input_schema={"type": "object"},
                ),
            )
            for kind in case["available"]
        }
    )
    plan = RuleRouter().route(context, capabilities)
    assert [kind.value for kind in plan.selected] == case["expectedRoute"]
    assert all(value not in repr(plan) for value in case.get("forbidden", []))
    if case["expectedStatus"] == "PARTIAL":
        report = RcaSynthesizer().insufficient_evidence(provider=case["provider"])
        assert report.status == "PARTIAL"
        assert case["requiredPhrase"] in report.summary_zh_tw
