import hmac
from collections.abc import Mapping
from typing import Protocol
from uuid import UUID

from pydantic import SecretStr


class SecretProvider(Protocol):
    def get_grafana_tokens(self, source_id: UUID) -> Mapping[str, SecretStr]: ...


class GrafanaUnauthorized(Exception):
    """Raised when a Grafana authorization header is missing or invalid."""


class GrafanaTokenAuthenticator:
    def __init__(self, secret_provider: SecretProvider) -> None:
        self._secret_provider = secret_provider

    def verify(self, source_id: UUID, authorization: str | None) -> str:
        credential = self._parse_bearer_credential(authorization)
        matching_token_id: str | None = None

        for token_id, token in self._secret_provider.get_grafana_tokens(
            source_id
        ).items():
            token_matches = hmac.compare_digest(credential, token.get_secret_value())
            if token_matches and matching_token_id is None:
                matching_token_id = token_id

        if matching_token_id is None:
            raise GrafanaUnauthorized("invalid Grafana authorization")

        return matching_token_id

    @staticmethod
    def _parse_bearer_credential(authorization: str | None) -> str:
        if authorization is None or not authorization.startswith("Bearer "):
            raise GrafanaUnauthorized("invalid Grafana authorization")

        credential = authorization.removeprefix("Bearer ")
        if (
            not credential
            or not credential.isascii()
            or any(character.isspace() for character in credential)
        ):
            raise GrafanaUnauthorized("invalid Grafana authorization")

        return credential


class ConfiguredGrafanaSecretProvider:
    def __init__(
        self,
        tokens: Mapping[UUID, Mapping[str, SecretStr]],
    ) -> None:
        self._tokens = {
            source_id: dict(source_tokens)
            for source_id, source_tokens in tokens.items()
        }

    def get_grafana_tokens(self, source_id: UUID) -> Mapping[str, SecretStr]:
        return self._tokens.get(source_id, {})
