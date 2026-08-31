import subprocess
import tempfile
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
ROLLOUT_PLAN = (
    ROOT
    / "docs"
    / "superpowers"
    / "plans"
    / "2026-08-28-immutable-migration-rollout-correction.md"
)
ROOT_README = ROOT / "README.md"
BACKEND_README = ROOT / "backend" / "README.md"
WORKER_README = ROOT / "rca-worker" / "README.md"
DEPLOY_README = ROOT / "deploy" / "k8s" / "README.md"

ALL_DOCS = (ROOT_README, BACKEND_README, WORKER_README, DEPLOY_README, SCHEMA_DOC)

VERSION_QUERIES = (
    "SELECT version_num FROM alembic_version_backend;",
    "SELECT version_num FROM alembic_version_rca_worker;",
)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _block_after(text: str, heading: str) -> str:
    assert heading in text, f"missing required documentation section: {heading}"
    return text.split(heading, maxsplit=1)[1]


def test_authoritative_migration_procedure_is_four_gated_and_safe() -> None:
    """受控維護窗口必須將四個明確 gate 與每一步雙版本檢查綁在一起。"""
    procedure = _block_after(_text(SCHEMA_DOC), "## 維護窗口與四個 migration gate")
    positions = [procedure.index(command) for command in FOUR_MIGRATION_COMMANDS]
    assert positions == sorted(positions)
    assert procedure.count("SELECT version_num FROM alembic_version_backend;") >= 4
    assert procedure.count("SELECT version_num FROM alembic_version_rca_worker;") >= 4
    for required in (
        "停止 Backend 與 RCA Worker 的所有 writes",
        "Worker-0003 的 postcondition catalog checks",
        "既有 Worker-0002 head",
        "只執行 gate 3 與 gate 4",
        "不得 stamp 或 replay",
        "不得執行 Alembic downgrade",
        "approved backup",
    ):
        assert required in procedure


def test_operator_docs_never_offer_upgrade_head_or_stamp() -> None:
    """任何 operator-visible 命令都不得跨越未驗證 migration gate。"""
    violations = [
        f"{doc.relative_to(ROOT)}"
        for doc in ALL_DOCS
        if "alembic upgrade head" in _text(doc) or "alembic stamp" in _text(doc)
    ]
    assert not violations, "operator docs contain unsafe migration commands: " + ", ".join(
        violations
    )


def test_root_operator_recovery_contract_names_all_protected_paths_and_policy() -> None:
    """人工 recovery 必須可執行，且不可意外重播或接受 payload override。"""
    text = _text(ROOT_README)
    for required in (
        "/api/v1/operations/outbox-events/{eventId}/retry",
        "/api/v1/operations/outbox-events/retry-pending?limit=100",
        "/api/v1/operations/outbox-events/retry-failed?limit=100",
        "authentication and global authorization",
        "no startup, periodic, or incidental backlog replay",
        "does not accept payload, topic, project, subscription, or scope overrides",
    ):
        assert required in text


def test_schema_doc_describes_one_final_uuid_only_runtime_schema() -> None:
    """canonical schema must be ordinary UUID-only tables; partitions are legacy only."""
    text = _text(SCHEMA_DOC)
    runtime_schema = _block_after(text, "## Final UUID-only runtime schema").split(
        "## Retained legacy partition parents", maxsplit=1
    )[0]
    for table in (
        "webhook_deliveries",
        "alert_events",
        "evidence_records",
        "incident_messages",
        "incident_timeline_events",
        "audit_events",
    ):
        assert f"CREATE TABLE {table}" in runtime_schema
    for required in (
        "id UUID PRIMARY KEY",
        "raw_result BYTEA",
        "metadata JSONB",
        "content_hash",
        "result_status",
        "relkind = 'r'",
        "historical rows are copied and validated before cutover",
    ):
        assert required in runtime_schema
    assert "PARTITION BY" not in runtime_schema
    assert "partition_timestamp" not in runtime_schema
    assert "raw_result_reference" not in runtime_schema

    legacy = _block_after(text, "## Retained legacy partition parents")
    expected_legacy = (
        "webhook_deliveries__partitioned_legacy_0003",
        "alert_events__partitioned_legacy_0003",
        "evidence_records__partitioned_legacy_0003",
        "incident_messages__partitioned_legacy_0003",
        "incident_timeline_events__partitioned_legacy_0003",
        "audit_events__partitioned_legacy_0003",
    )
    for table in expected_legacy:
        assert table in legacy
    assert "autevents__partitioned_legacy_0003" not in text


def test_task7_catalog_gate_checks_public_top_level_legacy_parents() -> None:
    """Task 7 catalog evidence must match PostgreSQL top-level parent metadata."""
    task7_catalog = _block_after(
        _text(ROLLOUT_PLAN), "task7_step5_catalog() {"
    ).split("task7_run_phase 'Step 5 catalog'", maxsplit=1)[0]
    canonical_tables = (
        "webhook_deliveries",
        "alert_events",
        "evidence_records",
        "incident_messages",
        "incident_timeline_events",
        "audit_events",
    )

    assert "JOIN pg_namespace AS n ON n.oid = c.relnamespace" in task7_catalog
    assert "WHERE n.nspname = 'public' AND c.relname IN (" in task7_catalog
    for table in canonical_tables:
        assert task7_catalog.count(f"'{table}'") == 1
        assert task7_catalog.count(f"'{table}__partitioned_legacy_0003'") == 1
    assert (
        "'$1 !~ /__partitioned_legacy_0003$/ && $2 == \"r\" && $3 == \"f\""
        in task7_catalog
    )
    assert (
        "'$1 ~ /__partitioned_legacy_0003$/ && $2 == \"p\" && $3 == \"f\""
        in task7_catalog
    )
    assert "$3 == \"t\"" not in task7_catalog
    assert task7_catalog.count("')\" -eq 6") == 2
    assert task7_catalog.count("')\" -eq 12") == 1


def test_task7_shutdown_is_term_first_bounded_and_retained_job_only() -> None:
    """Task 7 must give a blocked subscriber one full pull window to stop."""
    shutdown = _block_after(
        _text(ROLLOUT_PLAN), "task7_shutdown_term_poll_attempts=35"
    ).split("task7_preflight() {", maxsplit=1)[0]

    assert "task7_shutdown_fallback_poll_attempts=5" in shutdown
    assert "task7_shutdown_poll_seconds=1" in shutdown
    assert "for task7_shutdown_signal in TERM INT; do" in shutdown
    assert (
        "TERM) task7_shutdown_attempt_limit=$task7_shutdown_term_poll_attempts"
        in shutdown
    )
    assert (
        "*) task7_shutdown_attempt_limit=$task7_shutdown_fallback_poll_attempts"
        in shutdown
    )
    ownership_check = shutdown.index(
        'task7_exact_job_active "$task7_shutdown_pid"'
    )
    signal_loop = shutdown.index("for task7_shutdown_signal in TERM INT; do")
    assert ownership_check < signal_loop
    assert 'jobs -pr | grep -Fx "$task7_job_pid"' in shutdown
    assert 'jobs -ps | grep -Fx "$task7_job_pid"' in shutdown
    assert (
        'kill -"$task7_shutdown_signal" "$task7_shutdown_pid"' in shutdown
    )
    assert (
        "for task7_shutdown_signal in TERM INT; do\n"
        '      task7_exact_job_active "$task7_shutdown_pid"'
        in shutdown
    )
    assert (
        "task7_shutdown_attempt=$((task7_shutdown_attempt + 1))\n"
        "      done\n"
        '      task7_exact_job_active "$task7_shutdown_pid"'
        in shutdown
    )
    for forbidden in ("pkill", "killall", "kill -KILL", "SIGKILL"):
        assert forbidden not in shutdown


def test_task7_shutdown_reaps_child_exiting_during_final_poll_sleep() -> None:
    """The post-window ownership check must observe the final sleep exit."""
    shutdown = _block_after(
        _text(ROLLOUT_PLAN), "task7_shutdown_term_poll_attempts=35"
    ).split("task7_preflight() {", maxsplit=1)[0]
    script = f"""
set +e
{shutdown}
task7_shutdown_term_poll_attempts=1
task7_shutdown_fallback_poll_attempts=1
task7_shutdown_poll_seconds=0.2
(
  trap 'printf "unexpected INT\\n" >&2; exit 99' INT
  trap '' TERM
  printf 'ready\\n' > "$1"
  sleep 0.1
) &
probe_pid=$!
while [ ! -s "$1" ]; do sleep 0.01; done
task7_shutdown_exact_pid "$probe_pid" 'synthetic-final-sleep'
"""
    with tempfile.TemporaryDirectory() as directory:
        ready = Path(directory) / "ready"
        result = subprocess.run(
            ["bash", "-c", script, "task7-shutdown-regression", str(ready)],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )

    assert result.returncode == 0, result.stderr
    assert "synthetic-final-sleep PID" in result.stdout
    assert "reaped with status 0" in result.stdout
    assert "unexpected INT" not in result.stderr


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
