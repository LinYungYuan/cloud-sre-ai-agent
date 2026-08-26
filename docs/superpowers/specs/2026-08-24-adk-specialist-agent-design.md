# RCA Worker 的 Google ADK Specialist Agent 設計

**日期：** 2026-08-24
**狀態：** 已核准／實作完成（待部署）
**範圍：** 僅限 `rca-worker/`

**Implementation plan：** [ADK Specialist Agent implementation plan](../plans/2026-08-24-adk-specialist-agent-implementation.md)

本規格的 production activation 仍受 rollout gate 控制，預設維持
`SPECIALIST_ANALYSIS_MODE=DISABLED`；migration、contract 與 security/evaluation
coverage 已完成，repository-wide verification 與 deployment activation 仍是明確的
rollout gate。ACTIVE 僅能在 SHADOW audit 與 rollback checks 通過後由部署流程啟用。

## 1. 摘要

RCA Worker 目前只在最終 RCA 綜合分析階段使用 Google ADK。Metrics、Trace 與 Log specialist 仍是 deterministic Python MCP adapter：探索 allowlisted tools、呼叫 MCP、建立 `EvidenceDraft`，最後產生固定且通用的 finding。三份 Specialist Skill 雖會載入 registry，但執行 specialist 時並未使用。

本次變更會將 Metrics、Trace 與 Log 改造成真正的 Google ADK Specialist Agent，同時不讓 LLM 直接、不受限制地操作 MCP。每個 ADK Specialist 載入對應 Skill，並且只能使用兩個針對單次執行建立的 Python tools：

1. `collect_evidence()`：僅呼叫該 specialist 被 allowlist 核准的 MCP capabilities，保存完整 evidence，然後回傳受大小限制的 receipt 與第一個分析 chunk。
2. `read_evidence_chunk(evidence_id, chunk_index)`：只讀取同一 RCA run、同一 specialist 所擁有的另一個受限 chunk。

既有 deterministic router 繼續負責 provider、safe scope、capability selection、執行順序、deadline 與失敗政策。Specialist Agent 產生引用已保存 evidence 的結構化 observations。Root RCA ADK Agent 只接收通過驗證的 observations 與 opaque evidence references，最後產生繁體中文 RCA 報告。

## 2. 目標

- 實作三個真正的 Google ADK Specialist Agents：Metrics、Trace 與 Log。
- 將 `metrics-analysis`、`trace-analysis`、`log-analysis` 分別載入對應 Agent instruction。
- 保留固定 endpoint 隔離、trusted capability manifest、read-only enforcement、schema validation、scope validation 與全域 deadline。
- MCP evidence 必須先完整保存，才可將任何內容提供給 Agent。
- 限制每次 Agent tool response 可包含的 telemetry 數量。
- 每一個 specialist observation 都必須引用同一 run、同一 specialist 擁有的 evidence。
- 並行執行被選取的 Specialist Agents，並保留部分成功結果。
- Root RCA Agent 接收具體且有 evidence 支持的 observations，不再只收到通用 MCP availability summary。
- 保存足夠的 Specialist analysis metadata，以稽核 model、Skill、輸出與 evidence references。

## 3. 非目標

- 不將原始 `McpToolset` 直接暴露給 LLM。
- 不允許 AlertValues、model output 或 MCP output 選擇 endpoint、tool name、scope、time window 或 mutation policy。
- 不允許 Specialist Agent 宣告最終 root cause 或 remediation action。
- 不自動執行 restart、rollback、scale、delete、deploy 或任何 mutation。
- 本次不新增 AWS MCP 支援。
- 不將 deterministic routing 移入 LLM。
- 除非未來需要顯示已保存的 Specialist analysis，否則不修改 Backend、Pub/Sub contract 或 Operator API response shape。

## 4. 變更前現況（baseline）

以下描述是本 implementation plan 的變更前基線；「目前」在這一節不表示實作後的
runtime 行為。

目前 production path：

```text
Pub/Sub job
  -> ProductionRcaProcessor
  -> 探索 MCP tools 並依 manifest 解析 capabilities
  -> RuleRouter 選取 Metrics / Trace / Log adapters
  -> adapters 並行呼叫 MCP
  -> PersistEvidence 保存 raw 與 structured evidence
  -> 固定 summary 加 evidence references
  -> AdkRcaAgent
  -> 保存 RCA report
```

變更前只有 `AdkRcaAgent` 會建立 `google.adk.agents.LlmAgent`。三個 specialist class 都
繼承 `McpSpecialist`，不會呼叫模型。`SdkMcpClient` 使用 MCP Python SDK 的
`ClientSession`；其中未實際使用的 `McpToolset` import 並不構成 ADK MCP integration。

實作後已由第 5 節目標架構取代此基線：Metrics、Trace、Log 分別建立真正的 ADK
Specialist Agent，並只取得 run-scoped `collect_evidence()` 與 `read_evidence_chunk()`；
Root RCA Agent 維持 `tools=[]`。變更前 deterministic adapters 的描述保留在此，供
deployment audit 區分 before/after。

## 5. 目標架構

```text
Pub/Sub job
  -> ProductionRcaProcessor
  -> deterministic scope 與 capability discovery
  -> RuleRouter 選取 specialists
  -> SpecialistAnalysisWorkflow（並行）
       -> Metrics ADK Agent + metrics-analysis
            -> 單次執行專用的 Metrics evidence tools
                 -> Metrics MCP -> 保存 evidence -> bounded chunks
       -> Trace ADK Agent + trace-analysis
            -> 單次執行專用的 Trace evidence tools
                 -> Trace MCP -> normalize -> 保存 evidence -> bounded chunks
       -> Log ADK Agent + log-analysis
            -> 單次執行專用的 Log evidence tools
                 -> Log MCP -> 保存 evidence -> bounded chunks
  -> 驗證並保存 specialist observations
  -> Root RCA ADK Agent + rca-analysis
  -> 驗證 citations 與 status
  -> 保存 RCA report
```

### 5.1 責任邊界

Google ADK 負責：

- 建立 Specialist 與 Root `LlmAgent`。
- 載入 Skill instructions。
- 約束模型的結構化輸出。
- 管理 model session 與 events。
- 執行 Specialist analysis 與 RCA synthesis。

Deterministic Python code 負責：

- Provider 與 safe scope authorization。
- Endpoint selection。
- Tool discovery 與 capability manifests。
- Read-only 與 schema checks。
- MCP request arguments。
- Deadlines 與 retry limits。
- Evidence persistence 與 ownership。
- Chunking 與 data budgets。
- Citation validation。
- Status transitions 與 database writes。

## 6. Skill 與 Capability Model

Specialist Skill capabilities 必須改用與 trusted MCP manifest 相同的 canonical names：

- Metrics：`metrics.query`
- Trace：`trace.query`
- Log：`log.query`

`anomaly-analysis`、`critical-path-analysis` 與 `pattern-analysis` 描述的是推理行為，不是 MCP capability，因此應寫在 Skill body，而不是 `required_capabilities`。

Capability discovery 必須從被選取 Specialist Skill 的 `required_capabilities` 取得需求，不再從所有 manifest entries 反向推導。只有當 Skill 要求的每個 capability 都精確對應一個 read-only、endpoint-bound 且 schema-matching 的 discovered tool 時，該 specialist 才能被 route。

RCA Skill 維持無 capability，因為 Root RCA Agent 只接收已保存的 observations，而且 `tools=[]`。

## 7. Specialist Agent Contract

新增不依賴 Google ADK 的 domain models：

```python
class SpecialistObservation(BaseModel):
    statement: str
    confidence: float
    relation: Literal["SUPPORTS", "CONTRADICTS", "MISSING"]
    evidence: tuple[EvidenceReference, ...]


class SpecialistAnalysisDraft(BaseModel):
    specialist: SpecialistKind
    status: Literal["COMPLETE", "PARTIAL", "FAILED"]
    observations: tuple[SpecialistObservation, ...]
    missing_evidence: tuple[str, ...]
```

驗證規則：

- `COMPLETE` 至少包含一個 observation，而且沒有 missing evidence。
- `PARTIAL` 至少包含一個 observation，或一個穩定的 missing-evidence code。
- `FAILED` 不得包含 observations。
- Confidence 必須介於 0 與 1 之間。
- 每個非 `MISSING` observation 至少引用一個已保存的 evidence reference。
- 每個 reference 必須屬於同一 RCA run 與同一 specialist。
- Specialist 不得引用其他 specialist 的 evidence。
- Specialist output 不得包含 remediation action 或 final root cause field。
- 若出現未知 reference，在 deadline 允許時最多進行一次安全的 corrective model retry。

## 8. 單次執行專用的 Evidence Tools

每個 Specialist Agent 都會取得針對單一 `SpecialistRequest` 建立的 tools。Agent 無法傳入 endpoint、project、provider、time window、任意 query 或 tool name。

### 8.1 `collect_evidence()`

此 tool 不接受模型控制的參數。Closure 持有 `incident_id`、`rca_run_id`、specialist kind、safe `CloudScope`、核准的 request window、allowlisted `AllowedTool` values、global deadline、persistence service 與 MCP client。

執行順序不得更動：

1. 再次驗證 safe GCP scope 與非空 allowed tools。
2. 只根據已核准 request 建立 MCP arguments。
3. 使用 manifest schema 驗證 arguments。
4. 只呼叫 endpoint-bound MCP client。
5. Normalize provider output。
6. 拒絕超過 raw-evidence byte budget 的 response。
7. 在 transaction 內保存完整 raw bytes 與 structured JSON。
8. Commit 並取得 `EvidenceReference`。
9. 建立受限制且 deterministic 的 analysis chunks。
10. 回傳 references、metadata、第一個 chunk、chunk 總數與 truncation status。

Database transaction commit 前，不得向模型回傳任何 evidence 內容。

此 tool 以 `(rca_run_id, specialist_type)` 保證 idempotency。同一模型重複呼叫時，回傳既有 receipt 與 chunks，不得再次發出 MCP network call。

### 8.2 `read_evidence_chunk(evidence_id, chunk_index)`

此 tool 只接受 opaque evidence ID 與非負整數 chunk index。Deterministic code 必須驗證 evidence 屬於目前 RCA run 與目前 specialist、index 沒有超過 chunk count，以及 global deadline 與 tool-call budget 尚未耗盡。

Tool 只回傳一個 bounded chunk 與其 evidence reference。它不能呼叫 MCP、改變 scope，或讀取其他 run 的 raw bytes。

### 8.3 不直接使用原始 `McpToolset` 的理由

直接暴露 `McpToolset` 會讓模型參與 endpoint、tool selection 與 argument decisions，也會使完整 evidence persistence、payload limits 與 ownership validation 更難可靠執行。單次執行專用的 Function Tools 可保留 ADK Agent 行為，同時維持既有 deterministic security 與 provenance boundary。

## 9. Telemetry 資料量控制

設定必須包含明確且為正數的上限。初始預設值：

- 每次 tool call 接受的 raw MCP response 上限：2 MiB。
- 每個 structured chunk 上限：8,000 個 Unicode code points。
- 每筆 evidence 最多向 Agent 提供 4 個 chunks。
- 每筆 evidence 最多向 Agent 提供 32,000 個 structured characters。
- 每個 Specialist Agent 最多呼叫 tools 5 次。
- 每個 Specialist output 最多 20 個 observations。
- Global RCA deadline：300 秒。
- 每個 Agent 最多 corrective model retry 1 次。

這些預設值必須是 validated startup settings，不得只寫在 prompt。Production 只能在文件定義的上限內覆寫為正數；若要提高上限，必須先執行 load test 與 model-context evaluation。

Chunking 規則：

- 相同 persisted structured JSON 與相同設定必須產生相同 chunks。
- Chunk boundary 不得混合不同 evidence records。
- 每個 chunk 都攜帶來源 evidence reference。
- Trace chunks 使用 normalized waterfall，並省略已遮罩 attributes。
- Metrics 與 Log chunks 在來源提供時保留原始排序與 timestamps。
- 若 chunk limit 省略部分已接受的 evidence，Specialist result 最高只能是 `PARTIAL`，並加入 `ANALYSIS_INPUT_TRUNCATED`。
- 若 raw response 超過 byte limit，不得將部分內容保存為有效 evidence，應回傳 `MCP_PAYLOAD_TOO_LARGE`。

當 trusted MCP schema 支援時，Worker 應優先使用 server-side query limit 或 pagination；這些數值不能由 Agent 決定。

## 10. ADK Adapter 設計

在 `AdkRcaAgent` 旁新增 `AdkSpecialistAgent` adapter。SDK imports 必須保留在 adapter methods 內，使 domain 與 application code 不依賴 Google ADK API。

Adapter 建立一個 `LlmAgent`，包含穩定且能區分 specialist 的 name、設定中的 model name、對應 Skill body、`SpecialistAnalysisDraft` output schema，以及僅有兩個單次執行專用的 evidence tools。不得提供 generic MCP toolset 或 mutation tools。

Prompt 將 AlertValues 表示為明確的 untrusted data field，並包含 specialist identity、核准 scope、核准 time window、allowed evidence reference format、output language requirements，以及 final root cause 與 remediation 僅能由 RCA synthesis 產生的規則。

Adapter 必須依 known persisted references 驗證 structured output。Corrective retry 只能包含穩定 validation code 與 allowed references，不得包含 raw tool errors 或模型產生的未知 IDs。

## 11. Orchestration 與資料流

`ProductionRcaProcessor` 繼續作為 deterministic application coordinator，不改成 LLM Agent。

1. 載入 incident context 與四份 Skills。
2. 使用 Skill-required capability names 探索 capabilities。
3. 以 deterministic router 選取 specialists。
4. 為每個被選取的 kind 建立單次執行專用 evidence tools 與 ADK Specialist Agent。
5. 在 global deadline 內並行執行被選取的 ADK Specialists。
6. 保存安全的 specialist failure codes 與成功的 analysis results。
7. 依 Metrics、Trace、Log 固定順序合併已驗證 observations。
8. 使用 observations 與 known evidence references 呼叫 Root RCA ADK Agent。
9. 任一被選取的 specialist 為 partial 或 failed 時，將名義上 complete 的 RCA report 降級為 `PARTIAL`。
10. 保存 final report 並 settle job。

這能確保 routing 不受 AlertValues 影響，同時讓四個推理元件都成為真正的 ADK Agents。

## 12. Persistence

透過新的 RCA Worker migration，在 `specialist_runs` 增加 analysis audit fields：

- `analysis_result JSONB NULL`
- `model_name TEXT NULL`
- `skill_name TEXT NULL`
- `skill_sha256 TEXT NULL`
- `analyzed_at TIMESTAMPTZ NULL`

新增 constraints：`analysis_result` 若非 NULL 必須是 JSON object；`skill_sha256` 若非 NULL 必須是小寫 SHA-256。

擴充既有 `specialist_runs.status` constraint，加入 `PARTIAL`；同時擴充 RCA Worker failure-code allowlist，加入新的 collection 與 analysis stable codes。Migration 必須保留全部既有合法值。

`analysis_result` 只保存驗證後的 `SpecialistAnalysisDraft`，不得複製 raw telemetry。Evidence 繼續保存在 `evidence_records`。保存 analysis 前，application validation 必須確認引用的 references 存在，並屬於同一 specialist run。

既有 `specialist_runs.status` 表示 specialist 的最終綜合狀態：

- `SUCCEEDED`：evidence collection 與 specialist analysis 完成。
- `PARTIAL`：存在可用 observations，但 evidence 缺失或遭截斷。
- `FAILED`：沒有可用 specialist observations。

## 13. 失敗處理

穩定的 specialist failure 與 missing-evidence codes：

- `NO_SAFE_MCP_CAPABILITY`
- `MCP_TIMEOUT`
- `MCP_TRANSPORT`
- `MCP_PAYLOAD_TOO_LARGE`
- `MCP_RESULT_INVALID`
- `ANALYSIS_TIMEOUT`
- `ANALYSIS_SCHEMA_INVALID`
- `ANALYSIS_UNKNOWN_EVIDENCE`
- `ANALYSIS_INPUT_TRUNCATED`
- `ANALYSIS_FAILED`

規則：

- Transport failure 在 global deadline 內最多 retry 一次。
- Schema、policy、payload-size 與 ownership failure 不得 retry MCP。
- Specialist model schema 或 citation failure 最多進行一次 corrective retry。
- 單一 Specialist failure 不得丟棄其他 Specialist 的 evidence 或 observations。
- 存在可用 observations，但任一被選取 Specialist 缺失或失敗時，產生 `PARTIAL` RCA report。
- 若沒有 specialist 可被安全 route，且沒有 evidence，產生既有誠實的 no-MCP `PARTIAL` report。
- 被 route 的 Specialists 全部永久失敗時，產生沒有 hypothesis 的 `FAILED` report。
- 不保存 exception text、raw model error 或 raw tool error。

## 14. 安全性質

- Telemetry、AlertValues、tool output 與 model output 都是不可信資料。
- Endpoint URLs 只能來自 validated startup settings。
- 模型不能選擇 tool name 或 endpoint，也不能擴大 scope 或 time window。
- MCP capabilities 必須 endpoint-bound 且 read-only。
- Network access 前必須再次驗證 arguments。
- Agent 引用 evidence 前，evidence 必須已 commit。
- Agent chunk reads 必須限制於同一 run 與同一 specialist。
- Root RCA synthesis 使用 `tools=[]`，Specialist Agent 也不得取得 mutation tool。
- Prompt injection 不能改變 deterministic routing 或 capability resolution。
- Persisted analysis 只引用 evidence references，不複製 raw payloads。

## 15. 測試策略

### 15.1 Unit tests

- Skill registry 載入 canonical specialist capabilities。
- Capability discovery 使用 Skill requirements，而不是所有 manifest entries。
- 每個 ADK Specialist 只收到對應 Skill 與對應 evidence tools。
- `collect_evidence()` 不接受 model-controlled scope、endpoint、query 或 tool name。
- 第一個 chunk 回傳前，evidence transaction 已完成 commit。
- 重複 collection call 保持 idempotent，不產生第二次 MCP call。
- 拒絕跨 run 與跨 specialist chunk access。
- Oversized response 必須 fail closed。
- Chunk count 與 chunk size 必須 bounded 且 deterministic。
- Trace chunks 保留 normalized waterfall redaction。
- Specialist observations 拒絕未知或跨 specialist evidence references。
- Alert 或 telemetry 的 prompt injection 不能改變 tools 或 routing。
- Corrective retry 只能使用 stable codes 與 allowed references。

### 15.2 Workflow tests

- 被選取的 ADK Specialist Agents 並行啟動。
- 缺少 capability 時只跳過對應 specialist。
- 單一 Specialist failure 保留其他 analyses，最終狀態為 `PARTIAL`。
- 全部被 route 的 Specialists 失敗時，最終狀態為 `FAILED`。
- Unsafe GCP 與 AWS scope 不呼叫 MCP，也不啟動 Specialist Agents。
- Deadline expiry 取消 pending calls，並阻止新的 tool calls。
- Root RCA prompt 收到具體 observations，不包含 raw telemetry 或 generic availability summaries。

### 15.3 Integration tests

- MCP discovery、collection、evidence persistence、analysis persistence 與 RCA report persistence 能在一次 job lifecycle 內完成。
- Evidence commit 後發生 crash 時，重用 persisted evidence，不重複執行 MCP collection。
- Database constraints 拒絕 malformed analysis audit data。
- Specialist observation references 可解析到正確的 persisted evidence partitions。
- 既有 Pub/Sub idempotency 與 lease recovery 維持有效。

### 15.4 Evaluation tests

- Metrics anomaly、Trace critical path 與 Log exception cases 都必須產生具體 observations 與 citations。
- 大型 telemetry fixtures 必須維持在 model-input budgets 內，截斷時回傳 `PARTIAL`。
- Specialist evidence 互相衝突時，`SUPPORTS` 與 `CONTRADICTS` relations 必須完整傳到 Root RCA Agent。
- Prompt injection fixtures 不得造成 endpoint change、mutation、虛構 evidence 或 unsupported root-cause claims。

## 16. Rollout

1. 在預設關閉的 configuration flag 後方新增 contracts、persistence migration 與單次執行專用 evidence tools。
2. 新增 Specialist ADK adapters 與 workflow tests。
3. 執行 shadow analysis：保留既有 report path，同時保存 Specialist analyses 供比較。
4. 評估 latency、token usage、observation quality、citation validity、payload truncation 與 failure rates。
5. 在非 production 環境啟用 Specialist analyses 作為 Root RCA input。
6. Migration、evaluation thresholds 與 rollback checks 通過後，再於 production 啟用。

Rollback 時停用 Specialist analysis input，回到既有 evidence-summary RCA path。Persisted evidence 與 optional analysis JSON 仍可讀取；application rollback 不需要 destructive downgrade。

## 17. 驗收標準

- Metrics、Trace 與 Log 都以 Google ADK `LlmAgent` 執行，並載入對應 Skill。
- Specialist 與 Root Agent 都不取得原始 `McpToolset`。
- 每個 Specialist 只能呼叫其單次執行專用、endpoint-isolated evidence tools。
- Evidence 回傳給 Agent 或被 Agent 引用前，必須已保存。
- 每個 Specialist observation 都引用屬於該 specialist 的 known evidence。
- RCA Agent 只接收具體且驗證過的 observations，不接收 raw telemetry。
- 大型 telemetry 受 byte、chunk 與 tool-call limits 約束。
- Partial failure 保留成功的 evidence 與 analyses。
- 既有 no-safe-scope 與 AWS paths 不呼叫 MCP 或模型，並維持誠實的 `PARTIAL` report。
- Unit、integration、contract、evaluation、lint 與 type checks 全部通過。
