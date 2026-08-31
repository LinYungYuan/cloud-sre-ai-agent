#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
kubectl_bin="${KUBECTL_BIN:-kubectl}"
migration_timeout="${MIGRATION_JOB_TIMEOUT:-15m}"

run_gate() {
  local manifest_name="$1"
  local gate_name="$2"
  local job_name

  printf 'Starting migration gate %s\n' "$gate_name"
  job_name=$("$kubectl_bin" create \
    -f "$script_dir/jobs/$manifest_name" \
    -o jsonpath='{.metadata.name}')
  if ! "$kubectl_bin" wait \
    --for=condition=complete \
    --timeout="$migration_timeout" \
    "job/$job_name"; then
    printf 'Migration gate %s failed\n' "$gate_name" >&2
    "$kubectl_bin" describe "job/$job_name" >&2 || true
    "$kubectl_bin" logs "job/$job_name" >&2 || true
    return 1
  fi
}

"$kubectl_bin" apply -f "$script_dir/base/serviceaccounts.yaml"
run_gate "backend-0002-migration-job.yaml" "Backend 0002"
run_gate "worker-0002-migration-job.yaml" "Worker 0002"
run_gate "backend-0003-migration-job.yaml" "Backend 0003"
run_gate "worker-0003-migration-job.yaml" "Worker 0003"

printf 'All four migration gates completed successfully\n'
