from pathlib import Path

import pytest

from sre_rca_worker.config.env_files import resolve_worker_env_file


def test_worker_resolver_uses_its_fixed_default_file_name(tmp_path: Path) -> None:
    expected = tmp_path / ".env.rca-worker"
    expected.touch()

    assert resolve_worker_env_file(environ={}, cwd=tmp_path) == expected


def test_worker_resolver_returns_none_when_its_default_file_is_absent(
    tmp_path: Path,
) -> None:
    assert resolve_worker_env_file(environ={}, cwd=tmp_path) is None


def test_worker_resolver_uses_an_existing_explicit_override(tmp_path: Path) -> None:
    expected = tmp_path / "worker.override.env"
    expected.touch()

    assert resolve_worker_env_file(
        environ={"RCA_WORKER_ENV_FILE": str(expected)}, cwd=tmp_path
    ) == expected


def test_worker_resolver_rejects_a_missing_explicit_override(tmp_path: Path) -> None:
    missing = tmp_path / "missing.env"

    with pytest.raises(FileNotFoundError):
        resolve_worker_env_file(
            environ={"RCA_WORKER_ENV_FILE": str(missing)}, cwd=tmp_path
        )


def test_worker_resolver_ignores_the_backend_override_variable(tmp_path: Path) -> None:
    backend_file = tmp_path / "backend.override.env"
    backend_file.touch()

    assert resolve_worker_env_file(
        environ={"BACKEND_ENV_FILE": str(backend_file)}, cwd=tmp_path
    ) is None
