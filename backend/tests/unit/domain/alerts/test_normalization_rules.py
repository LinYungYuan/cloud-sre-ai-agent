from uuid import UUID

import pytest
from pydantic import ValidationError

from sre_agent.domain.alerts.normalization import (
    CanonicalBaseAlert,
    NormalizationRule,
    NormalizationStatus,
    RuleCondition,
    RuleOutput,
    SafeRuleEngine,
)
from sre_agent.domain.alerts.provider import Provider, detect_provider

RULE_A = UUID("10000000-0000-0000-0000-000000000001")
RULE_B = UUID("10000000-0000-0000-0000-000000000002")


def _alert() -> CanonicalBaseAlert:
    return CanonicalBaseAlert(
        labels={
            "folder": "COM-LX-BOA-01",
            "alertname": "High CPU usage",
            "DBInstanceIdentifier": "production-rds-01",
            "Series": "123456789012",
        },
        annotations={"AlertValues": "Value: 85.23%\n<br>"},
        values={"B": 85.23},
    )


def _rule(
    rule_id: UUID = RULE_A,
    *,
    priority: int = 10,
    provider: Provider = Provider.AWS,
) -> NormalizationRule:
    return NormalizationRule(
        id=rule_id,
        name="database-cpu",
        version=1,
        priority=priority,
        conditions=(
            RuleCondition(path="labels.DBInstanceIdentifier", operator="exists"),
            RuleCondition(path="labels.folder", operator="prefix", value="COM-"),
            RuleCondition(path="labels.alertname", operator="equals", value="High CPU usage"),
            RuleCondition(path="labels.Series", operator="format", value="aws_account_id"),
        ),
        output=RuleOutput(
            provider=provider,
            resource_type="rds_instance",
            scope_path="labels.Series",
            resource_id_path="labels.DBInstanceIdentifier",
            resource_name_path="labels.DBInstanceIdentifier",
            display_unit="percent",
        ),
    )


def test_safe_rule_normalizes_only_declarative_paths_and_formats() -> None:
    result = SafeRuleEngine((_rule(),)).normalize(_alert(), detect_provider({}))

    assert result.status is NormalizationStatus.NORMALIZED
    assert result.rule_id == RULE_A
    assert result.rule_version == 1
    assert result.warnings == ()
    assert result.resource is not None
    assert result.resource.provider is Provider.AWS
    assert result.resource.resource_type == "rds_instance"
    assert result.resource.scope_id == "123456789012"
    assert result.resource.resource_id == "production-rds-01"


def test_equal_priority_matches_are_unclassified() -> None:
    result = SafeRuleEngine((_rule(), _rule(RULE_B))).normalize(
        _alert(), detect_provider({})
    )

    assert result.status is NormalizationStatus.UNCLASSIFIED
    assert result.resource is None
    assert result.warnings == ("normalization_rule_conflict",)


def test_rule_cannot_override_detected_provider() -> None:
    result = SafeRuleEngine((_rule(provider=Provider.GCP),)).normalize(
        _alert(), detect_provider({})
    )

    assert result.status is NormalizationStatus.UNCLASSIFIED
    assert result.resource is None
    assert result.warnings == ("normalization_provider_conflict",)


@pytest.mark.parametrize(
    "condition",
    [
        {"path": "labels.x", "operator": "exec", "value": "print(1)"},
        {"path": "__import__('os').system", "operator": "exists"},
        {"path": "labels.x; DROP TABLE incidents", "operator": "equals", "value": "x"},
        {"path": "labels.x", "operator": "format", "value": "(?s).*"},
        {"path": "labels.x", "operator": "equals", "value": None},
    ],
)
def test_rule_conditions_reject_executable_or_unbounded_input(
    condition: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        RuleCondition.model_validate(condition)


def test_rule_contracts_forbid_unknown_fields_and_invalid_outputs() -> None:
    with pytest.raises(ValidationError):
        RuleCondition.model_validate(
            {"path": "labels.x", "operator": "exists", "script": "return true"}
        )
    with pytest.raises(ValidationError):
        RuleOutput.model_validate({"provider": "AWS", "resource_type": ""})
    with pytest.raises(ValidationError):
        NormalizationRule.model_validate(
            {
                "id": str(RULE_A),
                "name": "empty",
                "version": 1,
                "priority": 1,
                "conditions": [],
                "output": {
                    "provider": "AWS",
                    "resource_type": "rds_instance",
                },
            }
        )
