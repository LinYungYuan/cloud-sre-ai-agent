import hmac
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr

from sre_agent.integrations.grafana.authenticator import (
    GrafanaTokenAuthenticator,
    GrafanaUnauthorized,
)


class FakeSecretProvider:
    def __init__(self, tokens: dict[str, SecretStr]) -> None:
        self.tokens = tokens

    def get_grafana_tokens(self, source_id: UUID) -> dict[str, SecretStr]:
        del source_id
        return self.tokens


@pytest.mark.parametrize(
    "authorization",
    [None, "", "Basic credentials", "bearer current-token", "Bearer  current-token", "Bearer "],
)
def test_verify_rejects_missing_or_non_exact_bearer_authorization(authorization: str | None):
    authenticator = GrafanaTokenAuthenticator(
        FakeSecretProvider({"current": SecretStr("current-token")})
    )

    with pytest.raises(GrafanaUnauthorized) as error:
        authenticator.verify(uuid4(), authorization)

    assert "current-token" not in str(error.value)


def test_verify_rejects_an_invalid_token_without_revealing_it():
    supplied_credential = "attacker-supplied-token"
    authenticator = GrafanaTokenAuthenticator(
        FakeSecretProvider({"current": SecretStr("current-token")})
    )

    with pytest.raises(GrafanaUnauthorized) as error:
        authenticator.verify(uuid4(), f"Bearer {supplied_credential}")

    assert supplied_credential not in str(error.value)


def test_verify_rejects_a_non_ascii_credential_without_revealing_it():
    supplied_credential = "caf\u00e9"
    authenticator = GrafanaTokenAuthenticator(
        FakeSecretProvider({"current": SecretStr("current-token")})
    )

    with pytest.raises(GrafanaUnauthorized) as error:
        authenticator.verify(uuid4(), f"Bearer {supplied_credential}")

    assert str(error.value) == "invalid Grafana authorization"
    assert supplied_credential not in str(error.value)


def test_verify_returns_the_non_secret_identifier_for_the_current_token():
    authenticator = GrafanaTokenAuthenticator(
        FakeSecretProvider({"current-2026-08": SecretStr("current-token")})
    )

    token_id = authenticator.verify(uuid4(), "Bearer current-token")

    assert token_id == "current-2026-08"
    assert token_id != "current-token"


def test_verify_accepts_a_rotated_token():
    authenticator = GrafanaTokenAuthenticator(
        FakeSecretProvider(
            {
                "current": SecretStr("current-token"),
                "previous": SecretStr("previous-token"),
            }
        )
    )

    token_id = authenticator.verify(uuid4(), "Bearer previous-token")

    assert token_id == "previous"


def test_verify_compares_every_configured_token_even_after_a_match(monkeypatch):
    configured = {
        "current": SecretStr("current-token"),
        "previous": SecretStr("previous-token"),
        "older": SecretStr("older-token"),
    }
    authenticator = GrafanaTokenAuthenticator(FakeSecretProvider(configured))
    compared: list[tuple[str, str]] = []
    original_compare = hmac.compare_digest

    def recording_compare(left: str, right: str) -> bool:
        compared.append((left, right))
        return original_compare(left, right)

    monkeypatch.setattr(
        "sre_agent.integrations.grafana.authenticator.hmac.compare_digest",
        recording_compare,
    )

    assert authenticator.verify(uuid4(), "Bearer current-token") == "current"
    assert compared == [
        ("current-token", "current-token"),
        ("current-token", "previous-token"),
        ("current-token", "older-token"),
    ]
