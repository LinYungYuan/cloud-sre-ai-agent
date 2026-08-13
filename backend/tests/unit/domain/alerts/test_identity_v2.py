from uuid import UUID

from sre_agent.domain.alerts.cross_cloud import AlertValidationError
from sre_agent.domain.alerts.identity import make_incident_identity_v2

SOURCE = UUID("00000000-0000-0000-0000-000000000001")


def test_identity_v2_uses_source_folder_and_alert_name() -> None:
    identity = make_incident_identity_v2(SOURCE, "COM-LX-BOA-01", "High CPU", "fp")

    assert identity.version == 2
    assert identity.parts == ("2", str(SOURCE), "COM-LX-BOA-01", "High CPU")
    assert len(identity.key) == 64
    assert identity.errors == ()


def test_length_prefix_encoding_prevents_delimiter_collisions() -> None:
    first = make_incident_identity_v2(SOURCE, "a:b", "c", "fp")
    second = make_incident_identity_v2(SOURCE, "a", "b:c", "fp")

    assert first.key != second.key


def test_invalid_identity_uses_fingerprint_fallback_without_merging_alerts() -> None:
    missing_folder = make_incident_identity_v2(SOURCE, None, "High CPU", "fp-1")
    missing_name = make_incident_identity_v2(SOURCE, "folder", " ", "fp-2")

    assert missing_folder.parts == ("2", str(SOURCE), "__invalid__", "fp-1")
    assert missing_name.parts == ("2", str(SOURCE), "__invalid__", "fp-2")
    assert missing_folder.key != missing_name.key
    assert missing_folder.errors == (
        AlertValidationError(field="folder", code="required"),
    )
    assert missing_name.errors == (
        AlertValidationError(field="alertname", code="required"),
    )


def test_invalid_identity_requires_a_non_blank_fingerprint() -> None:
    identity = make_incident_identity_v2(SOURCE, None, None, " ")

    assert identity.parts == ("2", str(SOURCE), "__invalid__", "__missing__")
    assert identity.errors == (
        AlertValidationError(field="folder", code="required"),
        AlertValidationError(field="alertname", code="required"),
        AlertValidationError(field="fingerprint", code="required"),
    )
