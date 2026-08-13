import pytest

from sre_agent.domain.alerts.severity import map_severity


@pytest.mark.parametrize("raw", ["ERROR", "error", " Error "])
def test_error_maps_to_sev1(raw: str) -> None:
    result = map_severity(raw)

    assert (result.raw, result.canonical, result.warnings) == (raw, "SEV1", ())


@pytest.mark.parametrize("raw", ["WARN", "warning", "WaRnInG", " warn "])
def test_warning_maps_to_sev3(raw: str) -> None:
    result = map_severity(raw)

    assert (result.raw, result.canonical, result.warnings) == (raw, "SEV3", ())


@pytest.mark.parametrize("raw", ["critical", "", None, 3, ["ERROR"]])
def test_unknown_or_non_string_severity_is_unmapped(raw: object) -> None:
    result = map_severity(raw)

    assert result.raw == (raw if isinstance(raw, str) else None)
    assert result.canonical == "UNMAPPED"
    assert result.warnings == ("severity_unmapped",)
