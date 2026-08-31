import json
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BASE_DIR = REPOSITORY_ROOT / "deploy" / "k8s" / "base"
JOBS_DIR = REPOSITORY_ROOT / "deploy" / "k8s" / "jobs"
DEPLOYMENT_RUNBOOK = REPOSITORY_ROOT / "deploy" / "k8s" / "README.md"

# outbox-deployment.yaml 與 partition-cronjob.yaml 已移除
EXPECTED_RESOURCE_FILES = {
    "configmap.yaml",
    "serviceaccounts.yaml",
    "backend-deployment.yaml",
    "backend-service.yaml",
    "frontend-deployment.yaml",
    "frontend-service.yaml",
    "worker-deployment.yaml",
}

RESOURCE_CONTRACTS = {
    "sre-agent-backend": {
        "requests": {"cpu": "100m", "memory": "256Mi"},
        "limits": {"cpu": "1000m", "memory": "1Gi"},
    },
    "sre-agent-frontend": {
        "requests": {"cpu": "25m", "memory": "64Mi"},
        "limits": {"cpu": "250m", "memory": "256Mi"},
    },
    "sre-agent-rca-worker": {
        "requests": {"cpu": "250m", "memory": "512Mi"},
        "limits": {"cpu": "2000m", "memory": "2Gi"},
    },
}


def _load_yaml(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as source:
        return [document for document in yaml.safe_load_all(source) if document]


def _load_resources() -> list[dict[str, Any]]:
    assert BASE_DIR.is_dir(), f"Kustomize base does not exist: {BASE_DIR}"
    documents: list[dict[str, Any]] = []
    for path in sorted(BASE_DIR.glob("*.yaml")):
        documents.extend(_load_yaml(path))
    return [document for document in documents if document.get("kind") != "Kustomization"]


def _resource(kind: str, name: str) -> dict[str, Any]:
    matches = [
        resource
        for resource in _load_resources()
        if resource["kind"] == kind and resource["metadata"]["name"] == name
    ]
    assert len(matches) == 1, f"expected one {kind}/{name}, found {len(matches)}"
    return matches[0]


def _pod_spec(resource: dict[str, Any]) -> dict[str, Any]:
    if resource["kind"] == "Deployment":
        return resource["spec"]["template"]["spec"]
    if resource["kind"] == "Job":
        return resource["spec"]["template"]["spec"]
    if resource["kind"] == "CronJob":
        return resource["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    raise AssertionError(f"resource does not own a Pod template: {resource['kind']}")


def _container(resource: dict[str, Any]) -> dict[str, Any]:
    containers = _pod_spec(resource)["containers"]
    assert len(containers) == 1
    return containers[0]


def _environment(container: dict[str, Any]) -> dict[str, dict[str, Any]]:
    assert "envFrom" not in container
    environment = container.get("env", [])
    names = [entry["name"] for entry in environment]
    assert len(names) == len(set(names)), "environment names must be unique"
    return {entry["name"]: entry for entry in environment}


def _assert_config_map_reference(entry: dict[str, Any], key: str) -> None:
    assert entry == {
        "name": entry["name"],
        "valueFrom": {
            "configMapKeyRef": {"name": "sre-agent-config", "key": key},
        },
    }


def _assert_secret_reference(entry: dict[str, Any], key: str) -> None:
    assert entry == {
        "name": entry["name"],
        "valueFrom": {
            "secretKeyRef": {"name": "sre-agent-secrets", "key": key},
        },
    }


def test_kustomization_references_each_base_manifest() -> None:
    kustomization_path = BASE_DIR / "kustomization.yaml"
    assert kustomization_path.is_file()
    documents = _load_yaml(kustomization_path)
    assert len(documents) == 1
    kustomization = documents[0]

    assert kustomization["apiVersion"] == "kustomize.config.k8s.io/v1beta1"
    assert kustomization["kind"] == "Kustomization"
    assert "namespace" not in kustomization
    assert set(kustomization["resources"]) == EXPECTED_RESOURCE_FILES
    assert len(kustomization["resources"]) == len(EXPECTED_RESOURCE_FILES)
    # 確認已刪除的資源不在 kustomization 中
    assert "outbox-deployment.yaml" not in kustomization["resources"], (
        "kustomization.yaml 不應含 outbox-deployment.yaml"
    )
    assert "partition-cronjob.yaml" not in kustomization["resources"], (
        "kustomization.yaml 不應含 partition-cronjob.yaml"
    )
    for relative_path in kustomization["resources"]:
        assert (BASE_DIR / relative_path).is_file()


def test_base_contains_only_portable_application_resources() -> None:
    resources = _load_resources()
    counts = Counter(resource["kind"] for resource in resources)

    # outbox Deployment 和 partition CronJob 已移除
    assert counts == {
        "ConfigMap": 1,
        "Deployment": 3,
        "Service": 2,
        "ServiceAccount": 3,
    }
    assert all("namespace" not in resource["metadata"] for resource in resources)
    assert not {"Secret", "Ingress", "Gateway"} & counts.keys()
    assert all(
        not resource["apiVersion"].startswith(
            ("gateway.networking.k8s.io/", "cloud.google.com/", "networking.gke.io/")
        )
        for resource in resources
    )

    service_accounts = [
        resource for resource in resources if resource["kind"] == "ServiceAccount"
    ]
    # sre-agent-outbox ServiceAccount 已移除
    assert {resource["metadata"]["name"] for resource in service_accounts} == {
        "sre-agent-backend",
        "sre-agent-migrator",
        "sre-agent-rca-worker",
    }
    assert all(not resource["metadata"].get("annotations") for resource in service_accounts)


def test_removed_outbox_and_partition_workloads_are_absent_from_base() -> None:
    """驗證 outbox Deployment 與 partition CronJob 已從 base 移除。"""
    resources = _load_resources()
    names = {resource["metadata"]["name"] for resource in resources}
    assert "sre-agent-outbox" not in names, (
        "sre-agent-outbox Deployment 不應出現在 base 資源中"
    )
    assert "sre-agent-partition-maintenance" not in names, (
        "sre-agent-partition-maintenance CronJob 不應出現在 base 資源中"
    )


def test_every_workload_has_hardened_non_root_security_and_resource_limits() -> None:
    workloads = [
        resource
        for resource in _load_resources()
        if resource["kind"] in {"Deployment", "CronJob"}
    ]
    assert {resource["metadata"]["name"] for resource in workloads} == set(
        RESOURCE_CONTRACTS
    )

    for workload in workloads:
        pod_spec = _pod_spec(workload)
        assert pod_spec["securityContext"]["runAsNonRoot"] is True
        assert pod_spec["securityContext"]["seccompProfile"] == {
            "type": "RuntimeDefault"
        }

        container = _container(workload)
        container_security = container["securityContext"]
        assert container_security["runAsNonRoot"] is True
        assert container_security["allowPrivilegeEscalation"] is False
        assert container_security["readOnlyRootFilesystem"] is True
        assert container_security["capabilities"] == {"drop": ["ALL"]}
        assert container["resources"] == RESOURCE_CONTRACTS[
            workload["metadata"]["name"]
        ]


def test_deployments_use_fixed_images_and_expected_rollout_contracts() -> None:
    expectations = {
        "sre-agent-backend": (2, "sre-agent-backend:latest", "sre-agent-backend"),
        "sre-agent-frontend": (2, "sre-agent-frontend:latest", None),
        "sre-agent-rca-worker": (
            1,
            "sre-agent-rca-worker:latest",
            "sre-agent-rca-worker",
        ),
    }
    for name, (replicas, image, service_account) in expectations.items():
        deployment = _resource("Deployment", name)
        assert deployment["spec"]["replicas"] == replicas
        assert deployment["spec"].get("strategy", {}).get("type") != "Recreate"
        assert _container(deployment)["image"] == image
        assert "/" not in image
        if service_account is None:
            assert "serviceAccountName" not in _pod_spec(deployment)
        else:
            assert _pod_spec(deployment)["serviceAccountName"] == service_account


def test_backend_owns_pubsub_publisher_config_but_excludes_ai_and_mcp_settings() -> None:
    """Backend 只擁有 Pub/Sub publisher 配置，不含 AI/MCP、分析模式、evidence 限制等 Worker 專屬設定。"""
    deployment = _resource("Deployment", "sre-agent-backend")
    container = _container(deployment)
    assert container["ports"] == [{"name": "http", "containerPort": 8000}]
    assert container["livenessProbe"]["httpGet"] == {"path": "/health/live", "port": "http"}
    assert container["readinessProbe"]["httpGet"] == {
        "path": "/health/ready",
        "port": "http",
    }

    environment = _environment(container)
    # Backend 應含 Pub/Sub publisher 配置
    assert "PUBSUB_PROJECT_ID" in environment, "Backend 必須含 PUBSUB_PROJECT_ID"
    assert "RCA_TOPIC_ID" in environment, "Backend 必須含 RCA_TOPIC_ID"
    assert "DATABASE_URL" in environment, "Backend 必須含 DATABASE_URL"
    assert "GRAFANA_TOKENS" in environment, "Backend 必須含 GRAFANA_TOKENS"

    # Worker 專屬 AI/MCP 設定不應出現在 Backend
    worker_only_keys = {
        "MODEL_NAME",
        "METRICS_MCP_URL",
        "TRACE_MCP_URL",
        "LOG_MCP_URL",
        "PUBSUB_SUBSCRIPTION_ID",
        "MCP_CAPABILITY_MANIFEST",
        "SPECIALIST_ANALYSIS_MODE",
        "RCA_DEADLINE_SECONDS",
        "EVIDENCE_CHUNK_CHARS",
        "EVIDENCE_MAX_CHUNKS",
        "EVIDENCE_MAX_TOTAL_CHARS",
        "SPECIALIST_MAX_TOOL_CALLS",
        "SPECIALIST_MAX_OBSERVATIONS",
        "AGENT_CORRECTIVE_RETRIES",
    }
    overlap = worker_only_keys & set(environment)
    assert not overlap, (
        f"Backend deployment 不應含 Worker 專屬設定：{sorted(overlap)}"
    )

    _assert_secret_reference(environment["DATABASE_URL"], "DATABASE_URL")
    _assert_secret_reference(environment["GRAFANA_TOKENS"], "GRAFANA_TOKENS")
    for key in set(environment) - {"DATABASE_URL", "GRAFANA_TOKENS"}:
        _assert_config_map_reference(environment[key], key)

    service = _resource("Service", "sre-agent-backend")
    assert service["spec"]["type"] == "ClusterIP"
    assert service["spec"]["selector"] == {"app": "sre-agent-backend"}
    assert service["spec"]["ports"] == [
        {"name": "http", "port": 8000, "targetPort": "http"}
    ]


def test_frontend_probes_runtime_config_service_and_writable_paths_are_wired() -> None:
    deployment = _resource("Deployment", "sre-agent-frontend")
    pod_spec = _pod_spec(deployment)
    assert pod_spec["automountServiceAccountToken"] is False
    container = _container(deployment)
    assert container["ports"] == [{"name": "http", "containerPort": 8080}]
    assert container["livenessProbe"]["httpGet"] == {"path": "/healthz", "port": "http"}
    assert container["readinessProbe"]["httpGet"] == {"path": "/healthz", "port": "http"}
    assert "env" not in container and "envFrom" not in container

    mounts = {mount["mountPath"]: mount for mount in container["volumeMounts"]}
    assert mounts["/usr/share/nginx/html/config.json"] == {
        "name": "runtime-config",
        "mountPath": "/usr/share/nginx/html/config.json",
        "subPath": "config.json",
        "readOnly": True,
    }
    assert {"/tmp", "/var/cache/nginx", "/var/run"} <= mounts.keys()

    volumes = {volume["name"]: volume for volume in pod_spec["volumes"]}
    assert volumes["runtime-config"] == {
        "name": "runtime-config",
        "configMap": {"name": "sre-agent-config", "items": [{"key": "config.json", "path": "config.json"}]},
    }
    for mount_path in ("/tmp", "/var/cache/nginx", "/var/run"):
        assert volumes[mounts[mount_path]["name"]]["emptyDir"] == {}

    service = _resource("Service", "sre-agent-frontend")
    assert service["spec"]["type"] == "ClusterIP"
    assert service["spec"]["selector"] == {"app": "sre-agent-frontend"}
    assert service["spec"]["ports"] == [
        {"name": "http", "port": 8080, "targetPort": "http"}
    ]


def test_worker_owns_subscriber_ai_and_mcp_settings_without_publisher_privileges() -> None:
    """Worker 擁有 subscriber、AI/MCP 配置，不含 Backend publisher 特有的 GRAFANA_TOKENS。"""
    deployment = _resource("Deployment", "sre-agent-rca-worker")
    pod_spec = _pod_spec(deployment)
    container = _container(deployment)
    assert deployment["spec"]["replicas"] == 1
    assert pod_spec["terminationGracePeriodSeconds"] >= 330
    assert container["command"] == ["sre-agent-rca-worker"]
    assert "ports" not in container
    assert "livenessProbe" not in container and "readinessProbe" not in container

    environment = _environment(container)
    # Worker 必須含 subscriber 與 AI/MCP 設定
    required_worker_keys = {
        "DATABASE_URL",
        "PUBSUB_PROJECT_ID",
        "PUBSUB_SUBSCRIPTION_ID",
        "APP_ENVIRONMENT",
        "MODEL_NAME",
        "METRICS_MCP_URL",
        "TRACE_MCP_URL",
        "LOG_MCP_URL",
    }
    missing = required_worker_keys - set(environment)
    assert not missing, f"Worker deployment 缺少必要設定：{sorted(missing)}"

    # Backend 特有的 Grafana token 不應出現在 Worker
    assert "GRAFANA_TOKENS" not in environment, (
        "Worker deployment 不應含 GRAFANA_TOKENS（Backend publisher 特有設定）"
    )

    _assert_secret_reference(environment["DATABASE_URL"], "DATABASE_URL")
    assert environment["WORKER_ID"] == {
        "name": "WORKER_ID",
        "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}},
    }
    for key in set(environment) - {"DATABASE_URL", "WORKER_ID"}:
        _assert_config_map_reference(environment[key], key)

    service_selectors = [
        service["spec"].get("selector", {})
        for service in _load_resources()
        if service["kind"] == "Service"
    ]
    assert all(
        selector.get("app") != "sre-agent-rca-worker" for selector in service_selectors
    )


def test_config_map_contains_only_approved_non_secret_defaults() -> None:
    config_map = _resource("ConfigMap", "sre-agent-config")
    data = config_map["data"]
    # ConfigMap 僅供 Worker 的 AI/MCP 設定，Backend 的 AI/MCP key 不應存在
    assert "APP_ENVIRONMENT" in data
    assert "PUBSUB_PROJECT_ID" in data
    assert "RCA_TOPIC_ID" in data
    assert "config.json" in data
    assert json.loads(data["config.json"]) == {
        "apiBaseUrl": "/api/v1",
        "locale": "zh-TW",
        "timeZone": "Asia/Taipei",
    }

    serialized = yaml.safe_dump(config_map).lower()
    for sensitive_term in (
        "database_url",
        "grafana_tokens",
        "credential",
        "password",
        "secretkeyref",
    ):
        assert sensitive_term not in serialized


def test_single_migration_job_runs_all_gates_in_order_with_least_privilege() -> None:
    assert {path.name for path in JOBS_DIR.glob("*.yaml")} == {"migration-job.yaml"}
    path = JOBS_DIR / "migration-job.yaml"
    assert path.is_file(), f"migration Job template does not exist: {path}"
    documents = _load_yaml(path)
    assert len(documents) == 1
    job = documents[0]

    assert job["apiVersion"] == "batch/v1"
    assert job["kind"] == "Job"
    assert job["metadata"]["generateName"] == "sre-agent-migration-"
    assert "name" not in job["metadata"]
    assert "namespace" not in job["metadata"]
    assert "data" not in job and "stringData" not in job

    assert job["spec"]["backoffLimit"] == 0
    assert job["spec"]["activeDeadlineSeconds"] == 1800
    assert job["spec"]["ttlSecondsAfterFinished"] == 86400

    pod_spec = _pod_spec(job)
    assert pod_spec["restartPolicy"] == "Never"
    assert pod_spec["serviceAccountName"] == "sre-agent-migrator"
    assert pod_spec["securityContext"] == {
        "runAsNonRoot": True,
        "runAsUser": 65532,
        "runAsGroup": 65532,
        "fsGroup": 65532,
        "seccompProfile": {"type": "RuntimeDefault"},
    }

    init_containers = pod_spec["initContainers"]
    assert [container["name"] for container in init_containers] == [
        "backend-0002",
        "worker-0002",
        "backend-0003",
        "worker-0003",
    ]
    assert [container["image"] for container in init_containers] == [
        "sre-agent-backend:latest",
        "sre-agent-rca-worker:latest",
        "sre-agent-backend:latest",
        "sre-agent-rca-worker:latest",
    ]
    assert [container["command"] for container in init_containers] == [
        ["alembic", "upgrade", "0002_grafana_normalization_v2"],
        ["alembic", "upgrade", "0002_adk_specialist_analysis"],
        ["alembic", "upgrade", "0003_non_partition_runtime_tables"],
        ["alembic", "upgrade", "0003_validate_ordinary_runtime_tables"],
    ]

    expected_resources = {
        "requests": {"cpu": "100m", "memory": "256Mi"},
        "limits": {"cpu": "1000m", "memory": "1Gi"},
    }
    expected_security_context = {
        "runAsNonRoot": True,
        "runAsUser": 65532,
        "runAsGroup": 65532,
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
    }
    for container in init_containers:
        environment = _environment(container)
        assert set(environment) == {"DATABASE_URL"}
        _assert_secret_reference(environment["DATABASE_URL"], "DATABASE_URL")
        assert container["securityContext"] == expected_security_context
        assert container["resources"] == expected_resources

    assert "upgrade\n    - head" not in yaml.safe_dump(job)
    assert "configMapKeyRef" not in yaml.safe_dump(job)

    completion = _container(job)
    assert completion["name"] == "complete"
    assert completion["image"] == "sre-agent-backend:latest"
    assert completion["command"] == [
        "python",
        "-c",
        "print('all migrations completed')",
    ]
    assert "env" not in completion and "envFrom" not in completion
    assert completion["securityContext"] == expected_security_context
    assert completion["resources"] == {
        "requests": {"cpu": "10m", "memory": "32Mi"},
        "limits": {"cpu": "100m", "memory": "128Mi"},
    }


def test_release_runbook_fails_fast_and_orders_migrations_before_rollouts() -> None:
    assert DEPLOYMENT_RUNBOOK.is_file(), (
        f"deployment runbook does not exist: {DEPLOYMENT_RUNBOOK}"
    )
    runbook = DEPLOYMENT_RUNBOOK.read_text(encoding="utf-8")
    ordered_release = runbook.split("## 依序發布", maxsplit=1)[1]
    release_commands = ordered_release.split("```bash", maxsplit=1)[1].split(
        "```", maxsplit=1
    )[0]
    ordered_commands = [
        "set -euo pipefail",
        "kubectl apply -f deploy/k8s/base/serviceaccounts.yaml",
        ("MIGRATION_JOB=$(kubectl create -f deploy/k8s/jobs/migration-job.yaml"),
        'kubectl wait --for=condition=complete --timeout=30m "job/${MIGRATION_JOB}"',
        "kubectl apply -k deploy/k8s/base",
        "kubectl rollout restart deployment/sre-agent-backend",
        "kubectl rollout restart deployment/sre-agent-frontend",
        "kubectl rollout restart deployment/sre-agent-rca-worker",
        "kubectl rollout status deployment/sre-agent-backend --timeout=5m",
        "kubectl rollout status deployment/sre-agent-frontend --timeout=5m",
        "kubectl rollout status deployment/sre-agent-rca-worker --timeout=5m",
    ]

    for command in ordered_commands:
        assert command in release_commands
    positions = [release_commands.index(command) for command in ordered_commands]
    assert positions == sorted(positions)

    # 確認 outbox 相關命令不在 runbook 中
    assert "sre-agent-outbox" not in release_commands, (
        "runbook 的依序發布區段不應含 sre-agent-outbox rollout 命令"
    )


def test_release_runbook_assigns_all_backend_gateway_paths() -> None:
    runbook = DEPLOYMENT_RUNBOOK.read_text(encoding="utf-8")
    routing = runbook.split("## 路由與正式環境限制", maxsplit=1)[1]

    assert "`/api`" in routing
    assert "`/webhooks/v1/grafana`" in routing
    assert "port 8000 的\n`sre-agent-backend` Service" in routing
