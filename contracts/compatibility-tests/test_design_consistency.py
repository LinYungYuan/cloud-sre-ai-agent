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
