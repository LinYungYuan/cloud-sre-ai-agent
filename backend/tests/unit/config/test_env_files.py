from pathlib import Path

import pytest

from sre_agent.config.env_files import resolve_backend_env_file


def test_backend_resolver_uses_its_fixed_default_file_name(tmp_path: Path) -> None:
    expected = tmp_path / ".env.backend-api"
    expected.touch()

    assert resolve_backend_env_file(environ={}, cwd=tmp_path) == expected


def test_backend_resolver_returns_none_when_its_default_file_is_absent(
    tmp_path: Path,
) -> None:
    assert resolve_backend_env_file(environ={}, cwd=tmp_path) is None


def test_backend_resolver_uses_an_existing_explicit_override(tmp_path: Path) -> None:
    expected = tmp_path / "backend.override.env"
    expected.touch()

    assert (
        resolve_backend_env_file(
            environ={"BACKEND_ENV_FILE": str(expected)}, cwd=tmp_path
        )
        == expected
    )


def test_backend_resolver_rejects_a_missing_explicit_override(tmp_path: Path) -> None:
    missing = tmp_path / "missing.env"

    with pytest.raises(FileNotFoundError):
        resolve_backend_env_file(
            environ={"BACKEND_ENV_FILE": str(missing)}, cwd=tmp_path
        )


def test_backend_resolver_ignores_the_worker_override_variable(tmp_path: Path) -> None:
    worker_file = tmp_path / "worker.override.env"
    worker_file.touch()

    assert (
        resolve_backend_env_file(
            environ={"RCA_WORKER_ENV_FILE": str(worker_file)}, cwd=tmp_path
        )
        is None
    )
