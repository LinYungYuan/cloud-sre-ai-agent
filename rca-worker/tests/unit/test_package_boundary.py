import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_worker_has_an_independent_import_root_and_project_files() -> None:
    for path in (
        ROOT / "pyproject.toml",
        ROOT / "uv.lock",
        ROOT / "alembic.ini",
        ROOT / "migrations/env.py",
        ROOT / "Dockerfile",
        ROOT / "src/sre_rca_worker/__init__.py",
    ):
        assert path.is_file(), path


def test_worker_never_imports_backend_source() -> None:
    violations: list[str] = []
    for path in (ROOT / "src").rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(name == "sre_agent" or name.startswith("sre_agent.") for name in names):
                violations.append(str(path.relative_to(ROOT)))

    assert violations == []
