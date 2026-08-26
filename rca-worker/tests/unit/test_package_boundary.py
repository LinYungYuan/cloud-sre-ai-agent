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
            if any(
                name == "sre_agent" or name.startswith("sre_agent.") for name in names
            ):
                violations.append(str(path.relative_to(ROOT)))

    assert violations == []


def _imported_modules(path: Path) -> list[str]:
    modules: list[str] = []
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.append(node.module or "")
    return modules


def test_domain_and_application_do_not_depend_on_google_adk() -> None:
    violations: list[str] = []
    for package in ("domain", "application"):
        for path in (ROOT / f"src/sre_rca_worker/{package}").rglob("*.py"):
            if any(
                module == "google.adk" or module.startswith("google.adk.")
                for module in _imported_modules(path)
            ):
                violations.append(str(path.relative_to(ROOT)))

    assert violations == []


def test_worker_source_never_uses_generic_mcp_toolsets() -> None:
    violations: list[str] = []
    for path in (ROOT / "src").rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        if any(
            (isinstance(node, ast.Name) and node.id == "McpToolset")
            or (isinstance(node, ast.Attribute) and node.attr == "McpToolset")
            or (
                isinstance(node, ast.alias)
                and (node.name == "McpToolset" or node.asname == "McpToolset")
            )
            for node in ast.walk(tree)
        ):
            violations.append(str(path.relative_to(ROOT)))

    assert violations == []


def test_specialist_adk_adapter_has_no_mcp_client_dependency() -> None:
    adapter = ROOT / "src/sre_rca_worker/agents/specialists/adk_agent.py"
    forbidden = {
        "sre_rca_worker.integrations.mcp.client",
        "sre_rca_worker.integrations.mcp.discovery",
        "sre_rca_worker.integrations.mcp.factories",
        "sre_rca_worker.integrations.mcp.sdk_client",
    }

    assert forbidden.isdisjoint(_imported_modules(adapter))


def test_specialist_adapter_keeps_google_imports_inside_methods() -> None:
    adapter = ROOT / "src/sre_rca_worker/agents/specialists/adk_agent.py"
    tree = ast.parse(adapter.read_text(), filename=str(adapter))
    top_level_imports = [
        module
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for module in (
            [alias.name for alias in node.names]
            if isinstance(node, ast.Import)
            else [node.module or ""]
        )
    ]

    assert not any(module.startswith("google") for module in top_level_imports)
