# ADK Specialist Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 將 Metrics、Trace、Log 從 deterministic MCP adapter 升級為真正的 Google ADK Specialist Agents；所有 telemetry 必須先保存、再以受限 chunk 提供分析，最後只把已驗證 observations 交給 Root RCA Agent。

**Architecture:** `ProductionRcaProcessor` 保留 deterministic coordinator 身分。Skill requirements 決定 capability discovery，run-scoped `EvidenceToolSession` 封裝 MCP collection、commit、idempotency、ownership 與 chunk budgets；`AdkSpecialistAgent` 僅取得 `collect_evidence()` 與 `read_evidence_chunk()`，三個 agents 由 workflow 並行執行。Root RCA 在 ACTIVE 模式只接收 structured observations，在 SHADOW 模式保存 specialist analyses 但沿用 legacy summary synthesis，在 DISABLED 模式完全走既有路徑。

**Tech Stack:** Python 3.11、Google ADK 2.7+、MCP Python SDK、Pydantic v2、SQLAlchemy async、PostgreSQL/JSONB、Alembic、pytest/pytest-asyncio、Ruff、Pyright。

**Spec:** `docs/superpowers/specs/2026-08-24-adk-specialist-agent-design.md`

## Global Constraints

- 不把 `McpToolset`、endpoint、tool name、scope、time window、任意 query 或 mutation tool 暴露給模型。
- MCP response 必須完整通過大小與格式驗證並 commit 後，才能將 receipt/chunk 回傳給 Agent。
- 每個 evidence reference 必須同時受 `rca_run_id` 與 `specialist_run_id` ownership 驗證。
- 預設 rollout mode 為 `DISABLED`；production 不因部署新版本自動切換資料路徑。
- 所有 exception、transport detail、raw model output 都不得保存為 failure text；只保存規格列出的 stable codes。
- 全部設定上限必須由 Pydantic 在 startup 驗證，且程式執行時再次 enforce，不能只寫入 prompt。
- 實作過程不得更改 Backend、Pub/Sub message contract 或 Operator API response shape。

---

## Task 1: Canonical Skill capabilities 與 Skill-driven discovery

**Files:**

- Modify: `rca-worker/src/sre_rca_worker/agents/skills/definitions/metrics-analysis/SKILL.md`
- Modify: `rca-worker/src/sre_rca_worker/agents/skills/definitions/trace-analysis/SKILL.md`
- Modify: `rca-worker/src/sre_rca_worker/agents/skills/definitions/log-analysis/SKILL.md`
- Modify: `rca-worker/src/sre_rca_worker/agents/skills/registry.py`
- Modify: `rca-worker/src/sre_rca_worker/integrations/mcp/discovery.py`
- Modify: `rca-worker/src/sre_rca_worker/application/rca/processor.py`
- Modify: `rca-worker/tests/unit/agents/skills/test_registry.py`
- Create: `rca-worker/tests/unit/integrations/mcp/test_discovery.py`

**Interfaces:**

```python
class SkillRegistry:
    def required_capabilities(
        self,
    ) -> Mapping[SpecialistKind, tuple[str, ...]]: ...

async def discover_capabilities(
    factory: McpClientFactory,
    scope: CloudScope | None,
    manifest: tuple[ManifestEntry, ...],
    required_by_specialist: Mapping[SpecialistKind, tuple[str, ...]],
) -> tuple[CapabilitySet, dict[SpecialistKind, McpClient]]: ...
```

- [ ] **Step 1: 先寫 Skill canonical capability 測試**

在 `test_registry.py` 將三個 specialist 的期望值明確固定：

```python
assert registry.required_capabilities() == {
    SpecialistKind.METRICS: ("metrics.query",),
    SpecialistKind.TRACE: ("trace.query",),
    SpecialistKind.LOG: ("log.query",),
}
assert registry.get_for_agent("rca").required_capabilities == ()
```

並驗證 `anomaly-analysis`、`critical-path-analysis`、`pattern-analysis` 只存在 Skill body，不存在 frontmatter capabilities。

- [ ] **Step 2: 寫 discovery 失敗測試**

新增 fake factory/client，manifest 同時放入 Skill 需要與不需要的 entries，驗證：

```python
capabilities, _ = await discover_capabilities(
    factory,
    safe_scope,
    manifest,
    {SpecialistKind.METRICS: ("metrics.query",)},
)
assert [tool.capability for tool in capabilities.for_specialist(SpecialistKind.METRICS)] == ["metrics.query"]
assert fake_client.list_tools_calls == 1
```

另測 Skill 要求的 capability 缺少 exact manifest/schema match 時該 specialist 為空，不得以其他 manifest entry 補位。

- [ ] **Step 3: 執行測試，確認因新介面尚未存在而失敗**

Run: `cd rca-worker && uv run pytest tests/unit/agents/skills/test_registry.py tests/unit/integrations/mcp/test_discovery.py -v`

Expected: FAIL，指出 `SkillRegistry.required_capabilities` 或 discovery 第四個參數不存在。

- [ ] **Step 4: 修改三份 Skill 與 registry**

將 frontmatter 改為：

```yaml
# metrics
required_capabilities: [metrics.query]
# trace
required_capabilities: [trace.query]
# log
required_capabilities: [log.query]
```

將推理行為寫回各自 body；例如 Metrics 明確要求 anomaly、趨勢、門檻分析，但不可宣告最終 root cause/remediation。Registry 以固定 mapping 將 agent name 轉成 `SpecialistKind`，拒絕 specialist Skill 要求其他 endpoint prefix 的 capability。

- [ ] **Step 5: 修改 discovery 與 processor call site**

`discover_capabilities()` 只解析 `required_by_specialist[kind]`，manifest 仍負責 endpoint、read-only、tool regex 與 schema trust。Processor 傳入 `self._skills.required_capabilities()`。

- [ ] **Step 6: 執行單元測試**

Run: `cd rca-worker && uv run pytest tests/unit/agents/skills/test_registry.py tests/unit/integrations/mcp/test_discovery.py tests/unit/integrations/mcp/test_capability_resolver.py -v`

Expected: PASS。

- [ ] **Step 7: Commit**

```bash
git add -- rca-worker/src/sre_rca_worker/agents/skills rca-worker/src/sre_rca_worker/integrations/mcp/discovery.py rca-worker/src/sre_rca_worker/application/rca/processor.py rca-worker/tests/unit/agents/skills/test_registry.py rca-worker/tests/unit/integrations/mcp/test_discovery.py
git commit -m "refactor: drive MCP discovery from specialist skills"
```

## Task 2: Rollout mode 與強制資料量設定

**Files:**

- Modify: `rca-worker/src/sre_rca_worker/config/settings.py`
- Modify: `rca-worker/tests/unit/config/test_settings.py`
- Modify: `deploy/k8s/base/configmap.yaml`
- Modify: `deploy/k8s/base/worker-deployment.yaml`

**Interfaces:**

```python
class SpecialistAnalysisMode(StrEnum):
    DISABLED = "DISABLED"
    SHADOW = "SHADOW"
    ACTIVE = "ACTIVE"

class WorkerSettings(BaseSettings):
    specialist_analysis_mode: SpecialistAnalysisMode = SpecialistAnalysisMode.DISABLED
    mcp_max_response_bytes: int = Field(default=2 * 1024 * 1024, gt=0, le=2 * 1024 * 1024)
    evidence_chunk_chars: int = Field(default=8_000, gt=0, le=8_000)
    evidence_max_chunks: int = Field(default=4, gt=0, le=4)
    evidence_max_total_chars: int = Field(default=32_000, gt=0, le=32_000)
    specialist_max_tool_calls: int = Field(default=5, gt=0, le=5)
    specialist_max_observations: int = Field(default=20, gt=0, le=20)
    rca_deadline_seconds: int = Field(default=300, gt=0, le=300)
    agent_corrective_retries: int = Field(default=1, ge=0, le=1)
```

- [ ] **Step 1: 寫 startup validation 測試**

驗證 default mode/limits、lowercase mode 被拒絕、零值被拒絕、production 嘗試提高任何 hard cap 被拒絕，並加入一致性驗證：

```python
assert settings.evidence_chunk_chars * settings.evidence_max_chunks <= settings.evidence_max_total_chars
```

- [ ] **Step 2: 執行測試，確認新欄位不存在**

Run: `cd rca-worker && uv run pytest tests/unit/config/test_settings.py -v`

Expected: FAIL，指出 settings attributes 不存在。

- [ ] **Step 3: 實作 enum、validated fields 與跨欄位 constraint**

`model_validator` 必須拒絕 `chunk_chars * max_chunks > max_total_chars`；所有 production override 只能縮小上限。不要建立互相矛盾的 `enabled`/`shadow` booleans。

- [ ] **Step 4: 將環境變數接到 K8s**

ConfigMap 寫入明確預設值；Deployment 逐項以 `configMapKeyRef` 注入 `SPECIALIST_ANALYSIS_MODE` 與 budgets。預設必須保持 `DISABLED`。

- [ ] **Step 5: 執行測試與 manifest 靜態檢查**

Run: `cd rca-worker && uv run pytest tests/unit/config/test_settings.py -v`

Expected: PASS。

Run: `rg -n "SPECIALIST_ANALYSIS_MODE|MCP_MAX_RESPONSE_BYTES|EVIDENCE_CHUNK_CHARS|SPECIALIST_MAX_TOOL_CALLS" deploy/k8s/base`

Expected: ConfigMap 與 Deployment 都能找到相同 key。

- [ ] **Step 6: Commit**

```bash
git add -- rca-worker/src/sre_rca_worker/config/settings.py rca-worker/tests/unit/config/test_settings.py deploy/k8s/base/configmap.yaml deploy/k8s/base/worker-deployment.yaml
git commit -m "feat: add bounded specialist analysis settings"
```

## Task 3: Specialist structured analysis domain contract

**Files:**

- Create: `rca-worker/src/sre_rca_worker/domain/evidence/analysis.py`
- Create: `rca-worker/tests/unit/domain/evidence/test_analysis.py`

**Interfaces:**

```python
StableSpecialistCode = Literal[
    "NO_SAFE_MCP_CAPABILITY", "MCP_TIMEOUT", "MCP_TRANSPORT",
    "MCP_PAYLOAD_TOO_LARGE", "MCP_RESULT_INVALID", "ANALYSIS_TIMEOUT",
    "ANALYSIS_SCHEMA_INVALID", "ANALYSIS_UNKNOWN_EVIDENCE",
    "ANALYSIS_INPUT_TRUNCATED", "ANALYSIS_FAILED",
]

class SpecialistObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    statement: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    relation: Literal["SUPPORTS", "CONTRADICTS", "MISSING"]
    evidence: tuple[EvidenceReference, ...] = ()

class SpecialistAnalysisDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    specialist: SpecialistKind
    status: Literal["COMPLETE", "PARTIAL", "FAILED"]
    observations: tuple[SpecialistObservation, ...] = Field(max_length=20)
    missing_evidence: tuple[StableSpecialistCode, ...] = ()
```

- [ ] **Step 1: 寫完整 model invariant 測試**

涵蓋：COMPLETE 必須有 observation 且無 missing code；PARTIAL 必須有 observation 或 missing code；FAILED 禁止 observations；非 MISSING relation 必須有 citation；MISSING 可無 citation；confidence 0..1；extra remediation/root-cause fields 被拒絕；最多 20 observations。

- [ ] **Step 2: 執行測試確認 import 失敗**

Run: `cd rca-worker && uv run pytest tests/unit/domain/evidence/test_analysis.py -v`

Expected: FAIL，module 尚不存在。

- [ ] **Step 3: 實作 frozen Pydantic models**

使用 `model_validator(mode="after")` 表達跨欄位規則，且 stable codes 只在單一 type alias 定義，後續 migration 與 workflow 依同一清單建立測試，不散落自由字串。

- [ ] **Step 4: 執行測試**

Run: `cd rca-worker && uv run pytest tests/unit/domain/evidence/test_analysis.py -v`

Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add -- rca-worker/src/sre_rca_worker/domain/evidence/analysis.py rca-worker/tests/unit/domain/evidence/test_analysis.py
git commit -m "feat: define specialist analysis contracts"
```

## Task 4: Specialist analysis audit migration

**Files:**

- Create: `rca-worker/migrations/versions/0002_adk_specialist_analysis.py`
- Modify: `rca-worker/tests/integration/persistence/test_schema.py`
- Modify: `docs/database/postgresql-schema.md`

**Interfaces / schema:**

```sql
ALTER TABLE specialist_runs
  ADD COLUMN analysis_result JSONB NULL,
  ADD COLUMN model_name TEXT NULL,
  ADD COLUMN skill_name TEXT NULL,
  ADD COLUMN skill_sha256 TEXT NULL,
  ADD COLUMN analyzed_at TIMESTAMPTZ NULL;
```

新 status constraint 保留 `QUEUED|RUNNING|SUCCEEDED|FAILED|SKIPPED` 並加入 `PARTIAL`。`rca_runs`、`specialist_runs`、`worker_attempts` 三張 Worker-owned tables 的 failure-code constraints 必須保留 0001 全部合法值，再加入本規格十個 stable specialist codes，避免相同 code 在不同 lifecycle 邊界寫入時被資料庫拒絕。

- [ ] **Step 1: 擴充 integration schema 測試**

驗證 Alembic head 為 `0002_adk_specialist_analysis`、五個 audit columns 存在、PARTIAL 可寫入、malformed JSON scalar 與非小寫 64-char SHA 被 DB constraint 拒絕，並驗證所有 legacy failure codes 仍合法。

- [ ] **Step 2: 執行 migration 測試，確認 revision 尚不存在**

Run: `cd rca-worker && MIGRATION_TEST_DATABASE_URL='postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/sre_agent_test' uv run pytest tests/integration/persistence/test_schema.py -v`

Expected: FAIL，缺 audit columns 或 version 仍為 0001。

- [ ] **Step 3: 實作 upgrade/downgrade**

Revision 設定：

```python
revision = "0002_adk_specialist_analysis"
down_revision = "0001_rca_worker_v1"
```

Upgrade 必須以 catalog 找出 legacy unnamed status CHECK 的實際名稱後 drop，再建立 Worker-owned named constraints；並重建三張 lifecycle tables 的 failure-code CHECK。`analysis_result` constraint 使用 `analysis_result IS NULL OR jsonb_typeof(analysis_result) = 'object'`；SHA constraint 使用 `skill_sha256 ~ '^[0-9a-f]{64}$'`。Downgrade 只移除本 revision 的 columns/constraints，恢復原 status/failure allowlist，不刪除 evidence 或 reports。

- [ ] **Step 4: 更新 schema 文件**

文件列出五欄、PARTIAL 語意、analysis JSON 不含 raw telemetry，以及完整 stable code allowlist。

- [ ] **Step 5: 從 0001 升級至 head 並執行測試**

Run: `cd rca-worker && POSTGRES_URL='postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/sre_agent_test' uv run alembic upgrade head`

Expected: exit 0，version 為 0002。

Run: `cd rca-worker && MIGRATION_TEST_DATABASE_URL='postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/sre_agent_test' uv run pytest tests/integration/persistence/test_schema.py -v`

Expected: PASS。

- [ ] **Step 6: Commit**

```bash
git add -- rca-worker/migrations/versions/0002_adk_specialist_analysis.py rca-worker/tests/integration/persistence/test_schema.py docs/database/postgresql-schema.md
git commit -m "feat: persist specialist analysis audit data"
```

## Task 5: Deterministic evidence chunking 與 payload fail-closed

**Files:**

- Create: `rca-worker/src/sre_rca_worker/domain/evidence/chunking.py`
- Create: `rca-worker/tests/unit/domain/evidence/test_chunking.py`
- Modify: `rca-worker/src/sre_rca_worker/agents/specialists/base.py`
- Modify: `rca-worker/tests/unit/agents/specialists/test_contracts.py`

**Interfaces:**

```python
class EvidenceChunk(BaseModel):
    reference: EvidenceReference
    chunk_index: int = Field(ge=0)
    chunk_count: int = Field(gt=0, le=4)
    content: str = Field(max_length=8_000)
    truncated: bool

def build_evidence_chunks(
    reference: EvidenceReference,
    structured_json: dict[str, Any] | list[Any],
    *, chunk_chars: int,
    max_chunks: int,
    max_total_chars: int,
) -> tuple[EvidenceChunk, ...]: ...

class McpPayloadTooLargeError(ValueError): ...
```

- [ ] **Step 1: 寫 chunking 測試**

使用含 emoji/CJK 的 fixtures 驗證以 Unicode code points 計數；相同 input/settings 產生完全相同 chunks；每個 chunk 不超過 8,000；最多四塊/32,000 chars；不同 evidence 不混塊；保留 JSON list 原始順序與 timestamp；超限時所有回傳 chunks 的 `truncated=True`。

- [ ] **Step 2: 寫 raw payload limit 測試**

為 `McpSpecialist` 增加 constructor `max_response_bytes`，fake MCP 回傳 `2 MiB + 1 byte` 時必須 raise `McpPayloadTooLargeError`，不得建立 `EvidenceDraft`。剛好 2 MiB 可進入 parsing（fixture 可用合法 JSON padding）。

- [ ] **Step 3: 執行測試確認失敗**

Run: `cd rca-worker && uv run pytest tests/unit/domain/evidence/test_chunking.py tests/unit/agents/specialists/test_contracts.py -v`

Expected: FAIL，chunking module/limit 尚未實作。

- [ ] **Step 4: 實作 canonical JSON serialization 與 deterministic slicing**

先以 `json.dumps(..., ensure_ascii=False, sort_keys=True, separators=(",", ":"))` 產生 canonical characters，再按 code point slice。`max_total_chars` 必須先限制，再切 chunks；不要用 bytes boundary。Trace 仍沿用既有 normalized waterfall，因此敏感 attributes 不會進入 chunks。

- [ ] **Step 5: 在 JSON parse 前 enforce raw bytes 上限**

`raw = await client.call(...)` 後第一個分支即檢查 `len(raw)`，超限拋 stable typed exception；不保存 partial bytes。將未使用的 `McpToolset` import 從 `integrations/mcp/sdk_client.py` 移除，MCP SDK boundary 只保留 `ClientSession`。

- [ ] **Step 6: 執行測試**

Run: `cd rca-worker && uv run pytest tests/unit/domain/evidence/test_chunking.py tests/unit/agents/specialists/test_contracts.py -v`

Expected: PASS。

- [ ] **Step 7: Commit**

```bash
git add -- rca-worker/src/sre_rca_worker/domain/evidence/chunking.py rca-worker/src/sre_rca_worker/agents/specialists/base.py rca-worker/src/sre_rca_worker/integrations/mcp/sdk_client.py rca-worker/tests/unit/domain/evidence/test_chunking.py rca-worker/tests/unit/agents/specialists/test_contracts.py
git commit -m "feat: bound and chunk persisted telemetry"
```

## Task 6: Run-scoped EvidenceToolSession（先 commit、idempotent、ownership-safe）

**Files:**

- Create: `rca-worker/src/sre_rca_worker/application/rca/evidence_tools.py`
- Modify: `rca-worker/src/sre_rca_worker/application/rca/persist_evidence.py`
- Modify: `rca-worker/src/sre_rca_worker/persistence/repositories/rca.py`
- Create: `rca-worker/tests/unit/application/test_evidence_tools.py`
- Create: `rca-worker/tests/integration/application/test_evidence_tools.py`

**Interfaces:**

```python
class EvidenceReceipt(BaseModel):
    specialist: SpecialistKind
    references: tuple[EvidenceReference, ...]
    first_chunks: tuple[EvidenceChunk, ...]
    total_chunks: int
    truncated: bool

class EvidenceToolSession:
    async def collect_evidence(self) -> EvidenceReceipt: ...
    async def read_evidence_chunk(
        self, evidence_id: UUID, chunk_index: int
    ) -> EvidenceChunk: ...
    @property
    def known_evidence(self) -> tuple[EvidenceReference, ...]: ...
```

Constructor 只由 application code 注入 `SpecialistRequest`、`specialist_run_id`、endpoint-bound collector、session factory、deadline 與全部 budgets。兩個 public methods 是唯一給 ADK 的 tool functions。

- [ ] **Step 1: 寫 unit security/budget tests**

驗證 public signature 沒有 endpoint/scope/query/tool name；第 6 次 tool call 回傳/拋 stable `ANALYSIS_FAILED`；deadline 過期阻止任何新 call；unsafe/AWS/no-tools 不建 MCP client；`read_evidence_chunk` 不會呼叫 MCP。

- [ ] **Step 2: 寫 integration transaction/idempotency tests**

使用真實 PostgreSQL transaction 與 counting fake client：

```python
receipt = await tools.collect_evidence()
assert await evidence_row_exists(receipt.references[0])
assert commit_observer.completed_before_tool_return is True

same = await tools.collect_evidence()
assert same == receipt
assert fake_client.calls == 1
```

另建同 run/不同 specialist、不同 run/same UUID query scenarios，驗證跨 ownership read 被 `ANALYSIS_UNKNOWN_EVIDENCE` 拒絕。模擬 evidence commit 後 process crash，建立新的 `EvidenceToolSession`，再次 collect 必須由 DB 重建 receipt 且 MCP calls 仍為 1。

- [ ] **Step 3: 執行測試確認缺少 service**

Run: `cd rca-worker && uv run pytest tests/unit/application/test_evidence_tools.py -v`

Expected: FAIL，module 尚不存在。

- [ ] **Step 4: 擴充 repository 的 ownership-aware read API**

```python
async def list_specialist_evidence(
    self, rca_run_id: UUID, specialist_run_id: UUID
) -> tuple[PersistedEvidence, ...]: ...

async def get_specialist_evidence(
    self, rca_run_id: UUID, specialist_run_id: UUID, evidence_id: UUID
) -> PersistedEvidence | None: ...
```

Query 必須同時比對 `rca_run_id`、`specialist_run_id`，並回傳 partition timestamp、structured JSON 與 metadata；不可只用 evidence UUID。

- [ ] **Step 5: 實作 collect transaction boundary**

以 per-session `asyncio.Lock` 防止模型重複/並行 collect。先查 DB reuse；沒有資料才執行 endpoint-bound collector。每個 draft 在同一 `session.begin()` 內 insert；離開 context（commit）後才呼叫 `build_evidence_chunks()` 並 return。若 collector 回傳多個 tools，總 MCP calls 仍受 `min(len(allowed_tools), 5)` 限制。

- [ ] **Step 6: 實作 chunk read 與 truncation signal**

每次從 persisted `structured_data` deterministic rebuild，不另存 raw chunk，不跨 evidence boundary。若任一 evidence truncated，receipt 加入 truncation flag，供 Specialist validator 將 status 限為 PARTIAL 並加 `ANALYSIS_INPUT_TRUNCATED`。

- [ ] **Step 7: 執行 unit + integration tests**

Run: `cd rca-worker && uv run pytest tests/unit/application/test_evidence_tools.py -v`

Expected: PASS。

Run: `cd rca-worker && MIGRATION_TEST_DATABASE_URL='postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/sre_agent_test' uv run pytest tests/integration/application/test_evidence_tools.py tests/integration/application/test_persist_evidence.py -v`

Expected: PASS。

- [ ] **Step 8: Commit**

```bash
git add -- rca-worker/src/sre_rca_worker/application/rca/evidence_tools.py rca-worker/src/sre_rca_worker/application/rca/persist_evidence.py rca-worker/src/sre_rca_worker/persistence/repositories/rca.py rca-worker/tests/unit/application/test_evidence_tools.py rca-worker/tests/integration/application/test_evidence_tools.py
git commit -m "feat: add run-scoped evidence tools"
```

## Task 7: Google ADK Specialist adapter 與 citation validator

**Files:**

- Create: `rca-worker/src/sre_rca_worker/agents/specialists/adk_agent.py`
- Create: `rca-worker/src/sre_rca_worker/agents/specialists/validator.py`
- Create: `rca-worker/tests/unit/agents/specialists/test_adk_agent.py`
- Create: `rca-worker/tests/unit/agents/specialists/test_validator.py`
- Modify: `rca-worker/tests/unit/test_package_boundary.py`

**Interfaces:**

```python
class SpecialistAnalysisValidator:
    def validate(
        self,
        draft: SpecialistAnalysisDraft,
        *,
        expected_specialist: SpecialistKind,
        owned_evidence: tuple[EvidenceReference, ...],
        input_truncated: bool,
    ) -> SpecialistAnalysisDraft: ...

class AdkSpecialistAgent:
    async def analyze(
        self,
        *,
        request: SpecialistRequest,
        evidence_tools: EvidenceToolSession,
        deadline: datetime,
    ) -> SpecialistAnalysisDraft: ...
```

- [ ] **Step 1: 寫 validator tests**

拒絕 wrong specialist、unknown UUID、正確 UUID 但 wrong partition、跨 specialist ownership；truncated input 將 COMPLETE 降為 PARTIAL 並加入 `ANALYSIS_INPUT_TRUNCATED`；最多 20 observations。驗證 correction error 對外只會是 `ANALYSIS_SCHEMA_INVALID` 或 `ANALYSIS_UNKNOWN_EVIDENCE`。

- [ ] **Step 2: 寫 adapter contract tests**

Subclass 覆寫 `_run_once()`，第一次回傳 unknown citation、第二次合法，驗證恰好一次 correction。檢查 prompt：AlertValues 是 `{rawText, untrusted: true}`；包含 specialist、approved scope/time window、allowed reference shape、zh-TW、禁止 final root cause/remediation；不包含 MCP URL、raw bytes、tool name 或 transport error。

另透過 injectable `_build_agent()` 或 monkeypatch `LlmAgent` 驗證：

```python
assert captured["name"] == "metrics_specialist_agent"
assert captured["instruction"] == metrics_skill.body
assert captured["output_schema"] is SpecialistAnalysisDraft
assert [tool.__name__ for tool in captured["tools"]] == [
    "collect_evidence", "read_evidence_chunk"
]
```

- [ ] **Step 3: 執行測試確認 adapter 尚不存在**

Run: `cd rca-worker && uv run pytest tests/unit/agents/specialists/test_validator.py tests/unit/agents/specialists/test_adk_agent.py -v`

Expected: FAIL，module 尚不存在。

- [ ] **Step 4: 實作 validator**

Known ownership set 使用 `(id, partition_timestamp)` exact pair。Validator 不查 DB；DB ownership 已由 EvidenceToolSession 建立 `owned_evidence`。任何模型額外欄位由 Pydantic `extra="forbid"` 擋下。

- [ ] **Step 5: 實作 ADK adapter**

所有 `google.adk` imports 留在 `_run_once()`。建立 `LlmAgent(mode="chat")`，tools 僅傳入兩個 bound async methods。與 Root adapter 相同使用 `InMemoryRunner`，在 remaining deadline 內收 final response，finally close runner。Correction prompt 只保留原始 approved context、stable code 與 `allowedEvidenceReferences`，不能帶回 unknown model ID。

- [ ] **Step 6: 加 package boundary 測試**

AST 測試確保 `domain/`、`application/` 沒有 `google.adk` imports，`McpToolset` 在整個 `rca-worker/src` 為零，且 specialist adapter 沒有 MCP client import。

- [ ] **Step 7: 執行測試**

Run: `cd rca-worker && uv run pytest tests/unit/agents/specialists/test_validator.py tests/unit/agents/specialists/test_adk_agent.py tests/unit/test_package_boundary.py -v`

Expected: PASS。

- [ ] **Step 8: Commit**

```bash
git add -- rca-worker/src/sre_rca_worker/agents/specialists/adk_agent.py rca-worker/src/sre_rca_worker/agents/specialists/validator.py rca-worker/tests/unit/agents/specialists/test_adk_agent.py rca-worker/tests/unit/agents/specialists/test_validator.py rca-worker/tests/unit/test_package_boundary.py
git commit -m "feat: add ADK specialist agents"
```

## Task 8: SpecialistAnalysisWorkflow 並行、deadline 與 partial preservation

**Files:**

- Create: `rca-worker/src/sre_rca_worker/agents/specialists/workflow.py`
- Modify: `rca-worker/src/sre_rca_worker/agents/rca/models.py`
- Create: `rca-worker/tests/unit/agents/specialists/test_workflow.py`

**Interfaces:**

```python
class SpecialistAnalysisResult(BaseModel):
    analysis: SpecialistAnalysisDraft
    known_evidence: tuple[EvidenceReference, ...]

class SpecialistAnalysisBundle(BaseModel):
    results: tuple[SpecialistAnalysisResult, ...] = ()
    failures: tuple[SpecialistFailure, ...] = ()

class SpecialistAnalysisWorkflow:
    async def run(
        self,
        context: IncidentContext,
        capabilities: CapabilitySet,
        *,
        deadline: datetime,
    ) -> SpecialistAnalysisBundle: ...
```

- [ ] **Step 1: 寫 workflow concurrency 測試**

沿用 barrier pattern，確認 Metrics/Trace/Log agents 在 barrier release 前都已啟動；輸出固定按 Metrics、Trace、Log，不依完成順序。

- [ ] **Step 2: 寫 routing/failure/deadline tests**

涵蓋缺 capability 只跳過對應 specialist；unsafe GCP/AWS 不建 EvidenceToolSession、不啟動 Agent；單一 `ANALYSIS_TIMEOUT` 保留其他 results；全部 routed specialists permanent failed 只回 failures；deadline 到期取消 pending calls並阻止後續 tools。Transport failure最多 retry一次，schema/payload/ownership不重試 MCP。

- [ ] **Step 3: 執行測試確認新 workflow 不存在**

Run: `cd rca-worker && uv run pytest tests/unit/agents/specialists/test_workflow.py -v`

Expected: FAIL。

- [ ] **Step 4: 實作 deterministic orchestration**

保留 `RuleRouter`；workflow factory 依 selected kind 建立 request、specialist run、EvidenceToolSession 與 AdkSpecialistAgent。使用 `asyncio.TaskGroup` 並以 global remaining seconds 包住每條 branch。將 typed collection/analysis exceptions 映射到規格 stable codes，不保存原始錯誤字串。

- [ ] **Step 5: 保留 durable transport retry 語意**

只有 MCP `ConnectionError/OSError` 可在 deadline 內 retry 一次；如果全部 branches 都是 `MCP_TRANSPORT`，processor 仍可依 job attempt 決定 NACK。模型 correction retry 由 adapter 負責，不由 workflow 再重跑整個 agent。

- [ ] **Step 6: 執行新舊 workflow tests**

Run: `cd rca-worker && uv run pytest tests/unit/agents/specialists/test_workflow.py tests/unit/agents/rca/test_workflow.py tests/unit/application/test_processor_retry.py -v`

Expected: PASS。

- [ ] **Step 7: Commit**

```bash
git add -- rca-worker/src/sre_rca_worker/agents/specialists/workflow.py rca-worker/src/sre_rca_worker/agents/rca/models.py rca-worker/tests/unit/agents/specialists/test_workflow.py
git commit -m "feat: orchestrate ADK specialists concurrently"
```

## Task 9: Root RCA 改用 observations，保留 shadow legacy input

**Files:**

- Modify: `rca-worker/src/sre_rca_worker/agents/rca/adk_agent.py`
- Modify: `rca-worker/src/sre_rca_worker/agents/rca/synthesizer.py`
- Modify: `rca-worker/tests/unit/agents/rca/test_synthesizer.py`

**Interfaces:**

```python
async def synthesize(
    self,
    *,
    alert_issue: str,
    specialist_analyses: tuple[SpecialistAnalysisDraft, ...],
    known_evidence: tuple[EvidenceReference, ...],
    deadline: datetime,
) -> RcaReportDraft: ...

def build_prompt(
    *,
    alert_issue: str,
    specialist_analyses: tuple[SpecialistAnalysisDraft, ...],
    known_evidence: tuple[EvidenceReference, ...],
) -> str: ...
```

為 SHADOW rollback 額外保留明確命名的 `synthesize_legacy(..., evidence_summaries=...)`，不要讓同一參數同時接受 summary 或 observation union。

- [ ] **Step 1: 寫 ACTIVE prompt tests**

驗證 prompt 含具體 statement/confidence/relation/evidence reference，且不含 `raw_result`、`structured_data`、MCP response 或 `"MCP 回傳可用觀測資料"` generic summary。Root `LlmAgent` 仍固定 `tools=[]`。

- [ ] **Step 2: 寫 partial downgrade tests**

任一 selected analysis 為 PARTIAL 或存在 specialist failure，即使 Root draft 為 COMPLETE，也要降為 PARTIAL；若三個成功且 Root citations 合法可維持 COMPLETE。`SUPPORTS`/`CONTRADICTS` 必須原樣出現在 Root prompt。

- [ ] **Step 3: 執行測試確認舊介面不符合**

Run: `cd rca-worker && uv run pytest tests/unit/agents/rca/test_synthesizer.py -v`

Expected: FAIL，`specialist_analyses` 尚未支援。

- [ ] **Step 4: 實作 observation-only prompt 與 legacy method**

ACTIVE path 只能 serialize `SpecialistAnalysisDraft.model_dump(mode="json")`。Correction retry 持續只使用 allowed refs 與 stable code。Legacy method 僅供 DISABLED/SHADOW rollout，並加 docstring 標示移除條件。

- [ ] **Step 5: 執行測試**

Run: `cd rca-worker && uv run pytest tests/unit/agents/rca/test_synthesizer.py -v`

Expected: PASS。

- [ ] **Step 6: Commit**

```bash
git add -- rca-worker/src/sre_rca_worker/agents/rca/adk_agent.py rca-worker/src/sre_rca_worker/agents/rca/synthesizer.py rca-worker/tests/unit/agents/rca/test_synthesizer.py
git commit -m "feat: synthesize RCA from specialist observations"
```

## Task 10: Processor composition、analysis persistence 與 rollout paths

**Files:**

- Modify: `rca-worker/src/sre_rca_worker/application/rca/processor.py`
- Modify: `rca-worker/src/sre_rca_worker/persistence/repositories/rca.py`
- Modify: `rca-worker/tests/unit/application/test_processor_retry.py`
- Modify: `rca-worker/tests/integration/application/test_production_processor.py`
- Modify: `rca-worker/tests/integration/application/test_persist_report.py`

**Interfaces:**

```python
async def upsert_specialist_analysis(
    self,
    *,
    rca_run_id: UUID,
    specialist: SpecialistKind,
    analysis: SpecialistAnalysisDraft,
    model_name: str,
    skill_name: str,
    skill_sha256: str,
) -> None: ...
```

Skill hash 使用 UTF-8 body 的小寫 SHA-256；`analysis_result` 只保存 validated draft JSON，不包含 receipt chunks/raw telemetry。

- [ ] **Step 1: 寫三種 rollout mode integration tests**

- DISABLED：不建立任何 Specialist ADK Agent，走既有 MCP collection + legacy RCA summary path。
- SHADOW：執行並保存 Specialist analyses，但 Root prompt 使用 legacy summaries；同一 evidence 不重複 MCP collect。
- ACTIVE：Root prompt 只收到 observations；`specialist_runs` 保存 status、analysis_result、model/skill/hash/analyzed_at。

使用 fake specialist/root adapter factory 注入 processor，避免 integration test 呼叫真模型。測試需檢查 `analysis_result::text` 不含 raw fixture secret。

- [ ] **Step 2: 寫狀態與 failure matrix tests**

```text
no safe route             -> RCA PARTIAL, no MCP, no specialist model
some observations + fail  -> RCA PARTIAL
all routed fail           -> RCA FAILED, no hypotheses
all analyses complete     -> Root result decides COMPLETE/PARTIAL
all MCP transport, try<3  -> raise ConnectionError for durable retry
```

Failure mapping只使用十個新 stable codes；不得再將 analysis error 誤標成 `INTERNAL_ERROR`。

- [ ] **Step 3: 執行 processor tests 確認新 composition 尚未存在**

Run: `cd rca-worker && MIGRATION_TEST_DATABASE_URL='postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/sre_agent_test' uv run pytest tests/unit/application/test_processor_retry.py tests/integration/application/test_production_processor.py tests/integration/application/test_persist_report.py -v`

Expected: FAIL，rollout mode/analysis audit 尚未接線。

- [ ] **Step 4: 將 processor 拆成清楚的 private paths**

```python
if mode is SpecialistAnalysisMode.DISABLED:
    return await self._run_legacy(claim, context, capabilities, clients)

bundle = await self._run_adk_specialists(...)
await self._persist_specialist_analyses(...)
if mode is SpecialistAnalysisMode.SHADOW:
    report = await self._synthesize_legacy_from_persisted_evidence(...)
else:
    report = await self._synthesize_from_observations(...)
```

不要在 ACTIVE branch 建立 legacy `Finding` generic summary。所有 branches 共用 final `_persist_report()` 與 job settlement。

- [ ] **Step 5: 實作 analysis audit transaction**

保存前以 repository 查詢每個 observation reference 的 `(rca_run_id, specialist_run_id, id, partition_timestamp)`。只有全部成立才 update specialist row。COMPLETE→`SUCCEEDED`、PARTIAL→`PARTIAL`、FAILED/no observations→`FAILED`。

- [ ] **Step 6: 維持 300 秒 durable deadline**

`RcaJobHandler._claim()` 目前以 `created_at + interval '5 minutes'` 產生 deadline；加入 unit/SQL assertion 使它與 `rca_deadline_seconds=300` default 一致。若未來允許縮短設定，claim 使用 `LEAST(created_at + interval '5 minutes', now() + make_interval(secs => :configured))`，不得超過 300 秒。

- [ ] **Step 7: 執行 unit + integration tests**

Run: `cd rca-worker && uv run pytest tests/unit/application/test_processor_retry.py -v`

Expected: PASS。

Run: `cd rca-worker && MIGRATION_TEST_DATABASE_URL='postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/sre_agent_test' uv run pytest tests/integration/application/test_production_processor.py tests/integration/application/test_persist_report.py -v`

Expected: PASS。

- [ ] **Step 8: Commit**

```bash
git add -- rca-worker/src/sre_rca_worker/application/rca/processor.py rca-worker/src/sre_rca_worker/persistence/repositories/rca.py rca-worker/tests/unit/application/test_processor_retry.py rca-worker/tests/integration/application/test_production_processor.py rca-worker/tests/integration/application/test_persist_report.py
git commit -m "feat: compose specialist analysis rollout paths"
```

## Task 11: End-to-end contract、security 與 evaluation coverage

**Files:**

- Create: `rca-worker/tests/contract/test_adk_specialist_boundaries.py`
- Create: `rca-worker/tests/eval/datasets/metrics-anomaly.json`
- Create: `rca-worker/tests/eval/datasets/trace-critical-path.json`
- Create: `rca-worker/tests/eval/datasets/log-exception-pattern.json`
- Create: `rca-worker/tests/eval/datasets/conflicting-observations.json`
- Create: `rca-worker/tests/eval/datasets/large-telemetry.json`
- Create: `rca-worker/tests/eval/datasets/prompt-injection.json`
- Modify: `rca-worker/tests/eval/test_rca_reports.py`
- Modify: `rca-worker/tests/contract/mcp/test_endpoint_isolation.py`

**Interfaces:** 測試只透過 public contracts 驗證，不依 ADK 網路服務；model calls 使用 deterministic fake adapter responses。

- [ ] **Step 1: 建立 specialist boundary contract tests**

驗證 Metrics/Trace/Log agent name、Skill name、tool names、endpoint isolation；模型輸入無 MCP URLs/tool schemas；Root tools 永遠空；任何 prompt injection 都不能改變 selected route、scope、time window 或 MCP arguments。

- [ ] **Step 2: 建立 evaluation fixtures 與 assertions**

- Metrics：輸出具體 anomaly statement + citation。
- Trace：critical path observation 引用 normalized/redacted trace。
- Log：exception pattern + timestamps/order citation。
- Conflict：SUPPORTS 與 CONTRADICTS 都傳入 Root。
- Large：所有 chunk/budget assertions 成立，status=PARTIAL，含 `ANALYSIS_INPUT_TRUNCATED`。
- Injection：不可出現 external URL、mutation、unknown evidence 或 unsupported root cause。

- [ ] **Step 3: 執行 contract/eval tests，先確認尚缺 coverage**

Run: `cd rca-worker && uv run pytest tests/contract/test_adk_specialist_boundaries.py tests/contract/mcp/test_endpoint_isolation.py tests/eval/test_rca_reports.py -v`

Expected: 新 tests 在 fixtures/behavior 實作完成前 FAIL。

- [ ] **Step 4: 補足 public behavior，禁止為測試放寬安全邊界**

只修正 adapter/workflow/validator 的公開行為；不得把 raw payload 加入 prompt、不得增加 generic tool、不得改成 model-controlled query。

- [ ] **Step 5: 執行 contract/eval tests**

Run: `cd rca-worker && uv run pytest tests/contract tests/eval -v`

Expected: PASS。

- [ ] **Step 6: Commit**

```bash
git add -- rca-worker/tests/contract rca-worker/tests/eval
git commit -m "test: cover ADK specialist security and evaluations"
```

## Task 12: 操作文件、全量驗證與 rollout gate

**Files:**

- Modify: `rca-worker/README.md`
- Modify: `docs/superpowers/specs/2026-08-24-adk-specialist-agent-design.md`

**Interfaces:** 文件必須給出 DISABLED → SHADOW → ACTIVE 的明確操作順序、觀測指標、rollback 指令/設定與啟用門檻。

- [ ] **Step 1: 更新 README 架構與設定表**

說明四個 ADK Agents、兩個 run-scoped tools、evidence-first commit、budgets、stable codes、三種 rollout modes。明確標示 AWS/no-safe-scope 不呼叫 MCP 或 Specialist model。

- [ ] **Step 2: 在 spec 記錄實作決策狀態**

將狀態由「提案」更新為「已核准／實作中」，加入 plan 連結；不改變已確認需求。

- [ ] **Step 3: 執行格式與靜態檢查**

Run: `cd rca-worker && uv run ruff format --check .`

Expected: PASS。

Run: `cd rca-worker && uv run ruff check .`

Expected: PASS。

Run: `cd rca-worker && uv run pyright`

Expected: `0 errors`。

- [ ] **Step 4: 執行完整測試套件**

先確保 migration test DB 已升級：

Run: `cd rca-worker && POSTGRES_URL='postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/sre_agent_test' uv run alembic upgrade head`

Expected: exit 0。

Run: `cd rca-worker && MIGRATION_TEST_DATABASE_URL='postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/sre_agent_test' uv run pytest -v`

Expected: 全部 PASS，無 skipped security/integration/eval tests。

- [ ] **Step 5: 做 forbidden-pattern 與 placeholder scan**

Run: `rg -n "McpToolset|TODO|TBD|NotImplementedError|analysis_result.*raw_result" rca-worker/src rca-worker/tests docs/superpowers/specs/2026-08-24-adk-specialist-agent-design.md`

Expected: 無 `McpToolset`、TODO、TBD、NotImplementedError；無 analysis JSON 複製 raw telemetry。若測試名稱或文件合理提到 forbidden term，逐筆人工確認而非忽略。

- [ ] **Step 6: 驗證 rollout gate**

在本機/測試環境依序跑：

1. `DISABLED`：既有 report path 結果不退化。
2. `SHADOW`：analysis audit 有值，Root report 仍來自 legacy path。
3. `ACTIVE`：Root prompt observation-only，latency < 300 秒，citation validity 100%，無 cross-owner read。
4. 將 mode 改回 `DISABLED`，確認不需要 destructive migration downgrade 即可 rollback。

- [ ] **Step 7: Commit 文件與最終修正**

```bash
git add -- rca-worker/README.md docs/superpowers/specs/2026-08-24-adk-specialist-agent-design.md
git commit -m "docs: document ADK specialist rollout"
```

- [ ] **Step 8: 最終工作樹與 commit 檢查**

Run: `git diff --check`

Expected: 無輸出。

Run: `git status --short`

Expected: 只允許使用者原本已有、且不屬於本計畫的 untracked diagram artifacts；本計畫相關檔案均已 commit。

## Completion Evidence

實作者完成後，交付訊息必須列出：

- 實際 migration revision 與 rollout default。
- 三個 Specialist Agent 各自載入的 Skill 與唯一兩個 tools。
- evidence commit-before-return、idempotency、ownership、chunk/tool-call limits 的測試名稱。
- Root ACTIVE prompt 不含 raw telemetry/generic summary 的測試名稱。
- Ruff、Pyright、unit、integration、contract、eval 的實際 command 與通過結果。
- 未執行的 production activation（ACTIVE）必須明確標示，不能宣稱已上線。
