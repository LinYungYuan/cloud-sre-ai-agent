from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

BACKEND_PYPROJECT = ROOT / "backend" / "pyproject.toml"
BACKEND_SRC = ROOT / "backend" / "src"

OBSOLETE_RUNTIME_MODULES = (
    "sre_agent/config/outbox_settings.py",
    "sre_agent/persistence/database.py",
    "sre_agent/workers/outbox_main.py",
    "sre_agent/workers/outbox_worker.py",
    "sre_agent/workers/partition_worker.py",
)

RETAINED_OUTBOX_MODULES = (
    "sre_agent/application/outbox/publish_events.py",
    "sre_agent/application/outbox/recover_events.py",
    "sre_agent/integrations/pubsub/publisher.py",
)


def test_platform_design_no_longer_contains_superseded_rca_or_chat_rules() -> None:
    text = (
        ROOT / "docs/superpowers/specs/2026-08-12-observability-rca-platform-design.md"
    ).read_text()

    for superseded in (
        "解析 team/project/environment/service scope",
        "team/environment/service 尚未完成分類",
        "本階段不實作 Router",
        "共享 AI 對話",
        "raw_result_reference",
    ):
        assert superseded not in text
    assert "resource.label.project_id" in text
    assert "folder + alertname" in text
    assert "Rule Router" in text
    assert "Chat" in text and "不屬於目前 release scope" in text


def test_obsolete_outbox_and_partition_entrypoints_are_removed() -> None:
    """驗證 backend/pyproject.toml 不再含 outbox-worker 或 ensure-partitions console script。"""
    pyproject_text = BACKEND_PYPROJECT.read_text()
    assert "sre-agent-outbox-worker" not in pyproject_text, (
        "backend/pyproject.toml 不應含 sre-agent-outbox-worker console script"
    )
    assert "sre-agent-ensure-partitions" not in pyproject_text, (
        "backend/pyproject.toml 不應含 sre-agent-ensure-partitions console script"
    )


def test_obsolete_outbox_and_partition_modules_are_not_in_backend_source() -> None:
    """驗證 backend src 已移除 OutboxSettings、partition_worker、outbox_main 等殘留。"""
    forbidden_patterns = (
        "OutboxSettings",
        "ensure_monthly_partitions",
        "PARTITIONED_TABLES",
        "outbox_main",
        "outbox_worker",
        "partition_worker",
    )
    violations: list[str] = []
    for py_file in BACKEND_SRC.rglob("*.py"):
        source = py_file.read_text(encoding="utf-8")
        for pattern in forbidden_patterns:
            if pattern in source:
                violations.append(f"{py_file.relative_to(ROOT)}: contains '{pattern}'")
    assert not violations, (
        "backend/src 不應含以下殘留符號：\n" + "\n".join(violations)
    )


def test_obsolete_runtime_modules_are_removed_without_deleting_outbox_services() -> None:
    """只移除 polling/partition runtime，保留 API 使用的發佈與復原邊界。"""
    obsolete_modules = [
        module
        for module in OBSOLETE_RUNTIME_MODULES
        if (BACKEND_SRC / module).exists()
    ]
    assert not obsolete_modules, (
        "backend/src 不應保留 obsolete runtime modules:\n"
        + "\n".join(obsolete_modules)
    )

    missing_retained_modules = [
        module
        for module in RETAINED_OUTBOX_MODULES
        if not (BACKEND_SRC / module).is_file()
    ]
    assert not missing_retained_modules, (
        "Task 4 不得刪除 API 仍使用的 outbox 發佈、復原或 Pub/Sub 邊界:\n"
        + "\n".join(missing_retained_modules)
    )


# ── Task 6 文件合約 ─────────────────────────────────────────────────────────────

FOUR_MIGRATION_COMMANDS = (
    "alembic upgrade 0002_grafana_normalization_v2",
    "alembic upgrade 0002_adk_specialist_analysis",
    "alembic upgrade 0003_non_partition_runtime_tables",
    "alembic upgrade 0003_validate_ordinary_runtime_tables",
)

ENV_EXAMPLE_FILES = (
    ".env.backend-api.example",
    ".env.rca-worker.example",
    ".env.backend-migration.example",
    ".env.rca-worker-migration.example",
    ".env.compose.example",
)

SCHEMA_DOC = ROOT / "docs" / "database" / "postgresql-schema.md"
ROOT_README = ROOT / "README.md"
BACKEND_README = ROOT / "backend" / "README.md"
WORKER_README = ROOT / "rca-worker" / "README.md"
DEPLOY_README = ROOT / "deploy" / "k8s" / "README.md"

ALL_DOCS = (ROOT_README, BACKEND_README, WORKER_README, DEPLOY_README, SCHEMA_DOC)


def test_all_four_migration_commands_appear_in_documentation() -> None:
    """四個明確 revision 命令必須出現在至少一份文件中；不得以 head 取代任何一個。"""
    combined = "\n".join(doc.read_text(encoding="utf-8") for doc in ALL_DOCS)
    missing = [cmd for cmd in FOUR_MIGRATION_COMMANDS if cmd not in combined]
    assert not missing, (
        "以下 migration 命令未出現在任何文件中：\n" + "\n".join(missing)
    )


def test_operator_docs_do_not_use_upgrade_head_for_migration_sequence() -> None:
    """migration 操作步驟不得含 'alembic upgrade head'；head 只允許在 docker run 示範行中出現。"""
    violations: list[str] = []
    for doc in ALL_DOCS:
        text = doc.read_text(encoding="utf-8")
        # 逐行掃描，排除 docker run 示範（僅允許 image 測試用途）
        for lineno, line in enumerate(text.splitlines(), 1):
            if "alembic upgrade head" in line and "docker run" not in line:
                violations.append(f"{doc.name}:{lineno}: {line.strip()}")
    assert not violations, (
        "operator 文件不得以 'alembic upgrade head' 作為 migration 步驟：\n"
        + "\n".join(violations)
    )


def test_schema_doc_contains_required_evidence_fields() -> None:
    """schema 文件必須記載精確的 evidence 欄位與 legacy partition 資料表。"""
    text = SCHEMA_DOC.read_text(encoding="utf-8")
    required_terms = (
        "raw_result BYTEA",
        "metadata JSONB",
        "content_hash",
        "result_status",
        "__partitioned_legacy_0003",
    )
    missing = [term for term in required_terms if term not in text]
    assert not missing, (
        "docs/database/postgresql-schema.md 缺少以下必要欄位說明：\n"
        + "\n".join(missing)
    )


def test_docs_do_not_reference_removed_runtime_commands() -> None:
    """文件不得含已刪除的 outbox worker 或 partition 啟動命令。"""
    forbidden = (
        "sre-agent-outbox-worker",
        "sre-agent-ensure-partitions",
        "sre_agent.workers.partition_worker",
    )
    violations: list[str] = []
    for doc in ALL_DOCS:
        text = doc.read_text(encoding="utf-8")
        for term in forbidden:
            if term in text:
                violations.append(f"{doc.name}: 含 '{term}'")
    assert not violations, (
        "文件不得引用已移除的 runtime 命令：\n" + "\n".join(violations)
    )


def test_root_readme_declares_all_env_example_files() -> None:
    """根 README 必須明確記載五個 .env.*.example 名稱。"""
    text = ROOT_README.read_text(encoding="utf-8")
    missing = [name for name in ENV_EXAMPLE_FILES if name not in text]
    assert not missing, (
        "README.md 缺少以下 env example 說明：\n" + "\n".join(missing)
    )
