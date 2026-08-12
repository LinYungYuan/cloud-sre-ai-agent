from datetime import UTC, datetime, timedelta
from uuid import UUID

from sre_agent.domain.alerts.classification import (
    AlertClassifier,
    AlertMapping,
    AlertScope,
    ScopeField,
    ScopeResolver,
)
from sre_agent.domain.alerts.models import ClassificationStatus

SOURCE = UUID("00000000-0000-0000-0000-000000000001")
OTHER_SOURCE = UUID("00000000-0000-0000-0000-000000000002")
TEAM = UUID("10000000-0000-0000-0000-000000000001")
PROJECT = UUID("20000000-0000-0000-0000-000000000001")
OTHER_PROJECT = UUID("20000000-0000-0000-0000-000000000002")
ENVIRONMENT = UUID("30000000-0000-0000-0000-000000000001")
SERVICE = UUID("40000000-0000-0000-0000-000000000001")


class KnownRecords(ScopeResolver):
    def __init__(self, records: dict[tuple[ScopeField, str], UUID]) -> None:
        self._records = records

    def resolve(self, field: ScopeField, label_value: str) -> UUID | None:
        return self._records.get((field, label_value))


def _known_records() -> KnownRecords:
    return KnownRecords(
        {
            ("team", "payments"): TEAM,
            ("project", "checkout"): PROJECT,
            ("environment", "production"): ENVIRONMENT,
            ("service", "api"): SERVICE,
        }
    )


def _scope(seed: int) -> AlertScope:
    return AlertScope(
        team_id=UUID(f"10000000-0000-0000-0000-{seed:012d}"),
        project_id=UUID(f"20000000-0000-0000-0000-{seed:012d}"),
        environment_id=UUID(f"30000000-0000-0000-0000-{seed:012d}"),
        service_id=UUID(f"40000000-0000-0000-0000-{seed:012d}"),
    )


def test_complete_known_labels_override_matching_mappings():
    mapping = AlertMapping(
        id=UUID("90000000-0000-0000-0000-000000000001"),
        source_id=SOURCE,
        priority=1,
        created_at=datetime(2026, 8, 12, tzinfo=UTC),
        scope=_scope(9),
    )
    classifier = AlertClassifier(SOURCE, _known_records(), [mapping])

    result = classifier.classify(
        labels={
            "team": "payments",
            "project": "checkout",
            "environment": "production",
            "service": "api",
        },
        rule_uid=None,
        folder=None,
    )

    assert result.status is ClassificationStatus.CLASSIFIED
    assert result.scope == AlertScope(TEAM, PROJECT, ENVIRONMENT, SERVICE)
    assert result.matched_mapping_id is None
    assert result.missing_fields == ()


def test_cloud_scope_id_is_the_canonical_project_label():
    classifier = AlertClassifier(
        SOURCE,
        KnownRecords(
            {
                ("team", "payments"): TEAM,
                ("project", "checkout-prod"): PROJECT,
                ("environment", "production"): ENVIRONMENT,
                ("service", "api"): SERVICE,
            }
        ),
        [],
    )

    result = classifier.classify(
        labels={
            "team": "payments",
            "cloud_scope_id": "checkout-prod",
            "environment": "production",
            "service": "api",
        },
        rule_uid=None,
        folder=None,
    )

    assert result.status is ClassificationStatus.CLASSIFIED
    assert result.scope == AlertScope(TEAM, PROJECT, ENVIRONMENT, SERVICE)
    assert result.missing_fields == ()


def test_cloud_scope_id_takes_precedence_over_a_legacy_project_label():
    classifier = AlertClassifier(
        SOURCE,
        KnownRecords(
            {
                ("team", "payments"): TEAM,
                ("project", "checkout-prod"): PROJECT,
                ("project", "checkout"): OTHER_PROJECT,
                ("environment", "production"): ENVIRONMENT,
                ("service", "api"): SERVICE,
            }
        ),
        [],
    )

    result = classifier.classify(
        labels={
            "team": "payments",
            "cloud_scope_id": "checkout-prod",
            "project": "checkout",
            "environment": "production",
            "service": "api",
        },
        rule_uid=None,
        folder=None,
    )

    assert result.status is ClassificationStatus.CLASSIFIED
    assert result.scope.project_id == PROJECT


def test_unknown_cloud_scope_id_does_not_fallback_to_a_legacy_project_label():
    classifier = AlertClassifier(SOURCE, _known_records(), [])

    result = classifier.classify(
        labels={
            "team": "payments",
            "cloud_scope_id": "unknown-project",
            "project": "checkout",
            "environment": "production",
            "service": "api",
        },
        rule_uid=None,
        folder=None,
    )

    assert result.status is ClassificationStatus.UNCLASSIFIED
    assert result.scope == AlertScope(TEAM, None, ENVIRONMENT, SERVICE)
    assert result.missing_fields == ("project",)


def test_partial_known_labels_are_preserved_and_completed_by_the_winning_mapping():
    mapping = AlertMapping(
        id=UUID("90000000-0000-0000-0000-000000000002"),
        source_id=SOURCE,
        priority=1,
        created_at=datetime(2026, 8, 12, tzinfo=UTC),
        scope=_scope(7),
        rule_uid="rule-1",
        folder="Payments",
        required_labels={"severity": "critical"},
    )
    classifier = AlertClassifier(SOURCE, _known_records(), [mapping])

    result = classifier.classify(
        labels={"team": "payments", "severity": "critical"},
        rule_uid="rule-1",
        folder="Payments",
    )

    assert result.status is ClassificationStatus.CLASSIFIED
    assert result.scope == AlertScope(
        team_id=TEAM,
        project_id=mapping.scope.project_id,
        environment_id=mapping.scope.environment_id,
        service_id=mapping.scope.service_id,
    )
    assert result.matched_mapping_id == mapping.id
    assert result.missing_fields == ()


def test_enabled_mappings_are_sorted_by_priority_created_at_and_id():
    created_at = datetime(2026, 8, 12, tzinfo=UTC)
    expected = AlertMapping(
        id=UUID("90000000-0000-0000-0000-000000000001"),
        source_id=SOURCE,
        priority=10,
        created_at=created_at,
        scope=_scope(1),
    )
    mappings = [
        AlertMapping(
            id=UUID("90000000-0000-0000-0000-000000000005"),
            source_id=SOURCE,
            priority=1,
            created_at=created_at - timedelta(days=1),
            scope=_scope(5),
            enabled=False,
        ),
        AlertMapping(
            id=UUID("90000000-0000-0000-0000-000000000004"),
            source_id=SOURCE,
            priority=20,
            created_at=created_at - timedelta(days=1),
            scope=_scope(4),
        ),
        AlertMapping(
            id=UUID("90000000-0000-0000-0000-000000000003"),
            source_id=SOURCE,
            priority=10,
            created_at=created_at + timedelta(days=1),
            scope=_scope(3),
        ),
        AlertMapping(
            id=UUID("90000000-0000-0000-0000-000000000002"),
            source_id=SOURCE,
            priority=10,
            created_at=created_at,
            scope=_scope(2),
        ),
        expected,
    ]
    classifier = AlertClassifier(SOURCE, KnownRecords({}), mappings)

    result = classifier.classify({}, rule_uid=None, folder=None)

    assert result.status is ClassificationStatus.CLASSIFIED
    assert result.scope == expected.scope
    assert result.matched_mapping_id == expected.id


def test_mapping_match_is_exact_for_source_rule_folder_and_required_labels():
    created_at = datetime(2026, 8, 12, tzinfo=UTC)
    winning = AlertMapping(
        id=UUID("90000000-0000-0000-0000-000000000005"),
        source_id=SOURCE,
        priority=5,
        created_at=created_at,
        scope=AlertScope(team_id=TEAM, project_id=PROJECT, environment_id=ENVIRONMENT),
        rule_uid="rule-1",
        folder="Payments",
        required_labels={"severity": "critical", "region": "tw"},
    )
    mismatches = [
        AlertMapping(
            id=UUID("90000000-0000-0000-0000-000000000001"),
            source_id=OTHER_SOURCE,
            priority=1,
            created_at=created_at,
            scope=_scope(1),
        ),
        AlertMapping(
            id=UUID("90000000-0000-0000-0000-000000000002"),
            source_id=SOURCE,
            priority=2,
            created_at=created_at,
            scope=_scope(2),
            rule_uid="rule-2",
        ),
        AlertMapping(
            id=UUID("90000000-0000-0000-0000-000000000003"),
            source_id=SOURCE,
            priority=3,
            created_at=created_at,
            scope=_scope(3),
            folder="Other",
        ),
        AlertMapping(
            id=UUID("90000000-0000-0000-0000-000000000004"),
            source_id=SOURCE,
            priority=4,
            created_at=created_at,
            scope=_scope(4),
            required_labels={"severity": "warning"},
        ),
    ]
    classifier = AlertClassifier(SOURCE, KnownRecords({}), [*mismatches, winning])

    result = classifier.classify(
        {"severity": "critical", "region": "tw"},
        rule_uid="rule-1",
        folder="Payments",
    )

    assert result.status is ClassificationStatus.UNCLASSIFIED
    assert result.scope == winning.scope
    assert result.matched_mapping_id == winning.id
    assert result.missing_fields == ("service",)


def test_unknown_or_absent_labels_without_a_mapping_remain_unclassified():
    classifier = AlertClassifier(SOURCE, _known_records(), [])

    result = classifier.classify(
        labels={"team": "unknown", "project": "checkout"},
        rule_uid=None,
        folder=None,
    )

    assert result.status is ClassificationStatus.UNCLASSIFIED
    assert result.scope == AlertScope(project_id=PROJECT)
    assert result.matched_mapping_id is None
    assert result.missing_fields == ("team", "environment", "service")
