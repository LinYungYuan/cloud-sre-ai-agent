# Google ADK Specialist Agents for RCA Worker

**Date:** 2026-08-24
**Status:** Proposed
**Scope:** `rca-worker/` only

## 1. Summary

The RCA Worker currently uses Google ADK only for the final RCA synthesis agent. Metrics, Trace, and Log specialists are deterministic Python MCP adapters: they discover allowlisted tools, invoke MCP, create `EvidenceDraft` values, and emit a generic fixed finding. Their three Skill definitions are loaded into the registry but are not used during specialist execution.

This change turns Metrics, Trace, and Log into Google ADK specialist agents without giving an LLM unrestricted MCP access. Each ADK specialist receives its corresponding Skill and exactly two run-scoped Python tools:

1. `collect_evidence()` invokes only that specialist's allowlisted MCP capabilities, persists exact evidence, and returns a bounded receipt plus the first analysis chunk.
2. `read_evidence_chunk(evidence_id, chunk_index)` returns another bounded chunk from evidence owned by the same RCA run and specialist.

The existing deterministic router remains the authority for provider, safe scope, capability selection, ordering, deadlines, and failure policy. Specialist agents produce structured observations referencing persisted evidence. The root RCA ADK agent receives only validated observations and opaque evidence references, then produces the final zh-TW RCA report.

## 2. Goals

- Implement three real Google ADK specialist agents: Metrics, Trace, and Log.
- Load `metrics-analysis`, `trace-analysis`, and `log-analysis` into the matching agent instructions.
- Preserve fixed endpoint isolation, trusted capability manifests, read-only enforcement, schema validation, scope validation, and global deadlines.
- Persist exact accepted MCP evidence before exposing any part of it to an agent.
- Bound the amount of telemetry returned in any agent tool response.
- Require every specialist observation to cite evidence owned by that run and specialist.
- Run selected specialist agents concurrently and retain partial successes.
- Give the root RCA agent concise, evidence-backed observations instead of generic MCP availability messages.
- Record enough specialist analysis metadata to audit the model, Skill, output, and evidence references.

## 3. Non-goals

- Do not expose raw `McpToolset` instances directly to an LLM.
- Do not let AlertValues, model output, or MCP output choose endpoints, tool names, scope, time windows, or mutation policy.
- Do not let specialist agents declare the final root cause or remediation action.
- Do not automate restart, rollback, scaling, deletion, deployment, or any mutation.
- Do not add AWS MCP support in this change.
- Do not move deterministic routing into an LLM.
- Do not redesign Backend, Pub/Sub contracts, or Operator API response shapes unless required to display stored specialist analysis later.

## 4. Current State

The current production path is:

```text
Pub/Sub job
  -> ProductionRcaProcessor
  -> discover MCP tools and resolve manifest capabilities
  -> RuleRouter selects Metrics / Trace / Log adapters
  -> adapters call MCP concurrently
  -> PersistEvidence saves raw and structured evidence
  -> fixed summaries plus evidence references
  -> AdkRcaAgent
  -> RCA report persistence
```

Only `AdkRcaAgent` constructs `google.adk.agents.LlmAgent`. The specialist classes inherit `McpSpecialist` and do not invoke a model. `SdkMcpClient` uses the MCP Python SDK's `ClientSession`; its unused `McpToolset` import does not create an ADK MCP integration.

## 5. Target Architecture

```text
Pub/Sub job
  -> ProductionRcaProcessor
  -> deterministic scope and capability discovery
  -> RuleRouter selects specialists
  -> SpecialistAnalysisWorkflow (concurrent)
       -> Metrics ADK Agent + metrics-analysis
            -> run-scoped Metrics evidence tools
                 -> Metrics MCP -> persist evidence -> bounded chunks
       -> Trace ADK Agent + trace-analysis
            -> run-scoped Trace evidence tools
                 -> Trace MCP -> normalize -> persist evidence -> bounded chunks
       -> Log ADK Agent + log-analysis
            -> run-scoped Log evidence tools
                 -> Log MCP -> persist evidence -> bounded chunks
  -> validate and persist specialist observations
  -> Root RCA ADK Agent + rca-analysis
  -> validate citations and status
  -> persist RCA report
```

### 5.1 Responsibility boundary

Google ADK owns:

- Specialist and root `LlmAgent` construction.
- Skill instructions.
- Structured model output.
- Model sessions and events.
- Specialist analysis and RCA synthesis.

Deterministic Python code owns:

- Provider and safe-scope authorization.
- Endpoint selection.
- Tool discovery and capability manifests.
- Read-only and schema checks.
- MCP request arguments.
- Deadlines and retry limits.
- Evidence persistence and ownership.
- Chunking and data budgets.
- Citation validation.
- Status transitions and database writes.

## 6. Skill and Capability Model

Specialist Skill capabilities must use the same canonical names as the trusted MCP manifest:

- Metrics: `metrics.query`
- Trace: `trace.query`
- Log: `log.query`

`anomaly-analysis`, `critical-path-analysis`, and `pattern-analysis` describe reasoning behavior, not MCP capabilities. They belong in the Skill body rather than `required_capabilities`.

Capability discovery must accept required capabilities from the selected specialist Skill instead of deriving them from all manifest entries. A specialist is routable only when every Skill-required capability resolves to exactly one read-only, endpoint-bound, schema-matching discovered tool.

The RCA Skill remains capability-free because the root RCA agent receives persisted observations and has no tools.

## 7. Specialist Agent Contract

Add structured domain models independent of Google ADK:

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

Validation rules:

- `COMPLETE` requires at least one observation and no missing evidence.
- `PARTIAL` requires at least one observation or a stable missing-evidence code.
- `FAILED` contains no observations.
- Confidence is between 0 and 1.
- Every non-`MISSING` observation cites at least one persisted evidence reference.
- Every cited reference belongs to the same RCA run and specialist.
- A specialist cannot cite another specialist's evidence.
- A specialist cannot output remediation actions or a final root cause field.
- Unknown references trigger one safe corrective model retry when the deadline permits.

## 8. Run-scoped Evidence Tools

Each specialist agent receives tools created for one `SpecialistRequest`. The agent cannot supply endpoint, project, provider, time window, arbitrary query, or tool name.

### 8.1 `collect_evidence()`

The tool has no model-controlled arguments. Its closure owns:

- `incident_id`
- `rca_run_id`
- specialist kind
- safe `CloudScope`
- approved request window
- allowlisted `AllowedTool` values
- global deadline
- persistence service
- MCP client

Execution order is mandatory:

1. Revalidate safe GCP scope and non-empty allowed tools.
2. Build MCP arguments only from the approved request.
3. Validate arguments against the manifest schema.
4. Call only the endpoint-bound MCP client.
5. Normalize provider output.
6. Reject a response that exceeds the accepted raw-evidence byte budget.
7. Persist exact raw bytes and structured JSON in a transaction.
8. Commit and obtain `EvidenceReference` values.
9. Build bounded deterministic analysis chunks.
10. Return references, metadata, the first chunk, total chunk count, and truncation status.

Evidence is never returned to the model before the database transaction commits.

The tool is idempotent per `(rca_run_id, specialist_type)`. Repeated model calls return the existing receipt and chunks; they do not repeat MCP network calls.

### 8.2 `read_evidence_chunk(evidence_id, chunk_index)`

This tool permits only an opaque evidence ID and non-negative chunk index. Deterministic code verifies:

- the evidence belongs to the current RCA run;
- the evidence belongs to the current specialist;
- the index is within the generated chunk count;
- the global deadline and per-agent tool-call budget remain available.

It returns one bounded chunk plus its evidence reference. It cannot query MCP, change scope, or retrieve raw bytes belonging to another run.

### 8.3 Why not raw `McpToolset`

Direct `McpToolset` exposure would let model tool selection participate in endpoint and argument decisions, complicate exact evidence persistence, and make payload limits and ownership validation harder to enforce. The run-scoped Function Tools preserve ADK agent behavior while keeping the existing security and provenance boundary deterministic.

## 9. Telemetry Volume Controls

Configuration must define explicit positive limits. The initial defaults are:

- maximum accepted raw MCP response per tool call: 2 MiB;
- maximum structured characters per chunk: 8,000 Unicode code points;
- maximum chunks exposed per evidence item: 4;
- maximum total structured characters exposed per evidence item: 32,000;
- maximum tool calls per specialist agent: 5;
- maximum observations per specialist output: 20;
- global RCA deadline: 300 seconds;
- corrective model retries per agent: 1.

These defaults are validated startup settings, not prompt text. Production may override them only with positive values inside documented upper bounds; increasing them requires load and model-context evaluation.

Chunking rules:

- Chunks are deterministic for the same persisted structured JSON and configuration.
- Chunk boundaries never mix evidence records.
- Every chunk carries the source evidence reference.
- Trace chunks use the normalized waterfall and omit redacted attributes.
- Metrics and Log chunks preserve source ordering and timestamps when available.
- If the chunk limit omits accepted evidence content, the specialist result is at most `PARTIAL` and includes `ANALYSIS_INPUT_TRUNCATED`.
- If the raw response exceeds the accepted byte limit, it is not partially persisted as valid evidence; return `MCP_PAYLOAD_TOO_LARGE`.

The Worker should prefer server-side query limits or pagination when the trusted MCP schema supports them. The Agent never chooses those values.

## 10. ADK Adapter Design

Add an `AdkSpecialistAgent` adapter next to `AdkRcaAgent`. SDK imports remain inside adapter methods so domain and application code do not depend on Google ADK APIs.

The adapter constructs one `LlmAgent` with:

- a stable specialist-specific name;
- configured model name;
- matching Skill body as instruction;
- `SpecialistAnalysisDraft` as output schema;
- exactly the two run-scoped evidence tools;
- no generic MCP toolset;
- no mutation tools.

The prompt represents AlertValues as an explicit untrusted data field and includes:

- specialist identity;
- approved scope description;
- approved time window;
- allowed evidence reference format;
- output language requirements;
- the rule that final root cause and remediation belong to RCA synthesis.

The adapter validates structured output against the known persisted references. A corrective retry contains only a stable validation code and allowed references, never raw tool errors or unknown model-generated IDs.

## 11. Orchestration and Data Flow

`ProductionRcaProcessor` remains the deterministic application coordinator. It does not become an LLM agent.

1. Load incident context.
2. Load all four Skills.
3. Discover capabilities using Skill-required capability names.
4. Route selected specialists deterministically.
5. Construct one run-scoped evidence toolset and one ADK specialist per selected kind.
6. Execute selected ADK specialists concurrently within the global deadline.
7. Persist safe specialist failure codes and successful analysis results.
8. Combine validated specialist observations in deterministic Metrics, Trace, Log order.
9. Invoke the root RCA ADK agent with observations and known evidence references.
10. Downgrade a nominally complete RCA report to `PARTIAL` when any selected specialist is partial or failed.
11. Persist the final report and settle the job.

This preserves the design rule that routing cannot be influenced by AlertValues while making all four reasoning components real ADK agents.

## 12. Persistence

Add analysis audit fields to `specialist_runs` through a new RCA Worker migration:

- `analysis_result JSONB NULL`
- `model_name TEXT NULL`
- `skill_name TEXT NULL`
- `skill_sha256 TEXT NULL`
- `analyzed_at TIMESTAMPTZ NULL`

Add checks that `analysis_result`, when present, is a JSON object and `skill_sha256`, when present, is a lowercase SHA-256 value.

Expand the existing `specialist_runs.status` constraint to allow `PARTIAL`, and expand the RCA Worker failure-code allowlist with the new stable collection and analysis codes. The migration must preserve all existing allowed values.

`analysis_result` stores only the validated `SpecialistAnalysisDraft`; it does not copy raw telemetry. Evidence remains in `evidence_records`. Application validation guarantees cited references exist and belong to the same specialist run before analysis is stored.

Existing `specialist_runs.status` remains the terminal combined specialist status:

- `SUCCEEDED`: evidence collection and specialist analysis completed.
- `PARTIAL`: usable observations exist but evidence was missing or truncated.
- `FAILED`: no usable specialist observations exist.

If the legacy database constraint does not allow `PARTIAL`, the migration must expand it without weakening other status constraints.

## 13. Failure Handling

Stable specialist failure and missing-evidence codes include:

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

Rules:

- Transport failures may retry once within the global deadline.
- Schema, policy, payload-size, and ownership failures do not retry MCP.
- Specialist model schema or citation failure may receive one corrective retry.
- One specialist failure never discards other specialist evidence or observations.
- Some usable observations plus any missing or failed selected specialist produce a `PARTIAL` RCA report.
- No usable evidence produces the existing honest `PARTIAL` no-MCP report when nothing was safely routable.
- Routed specialists that all fail permanently produce `FAILED` with no hypothesis.
- Exceptions and raw model/tool error text are not persisted.

## 14. Security Properties

- Telemetry, AlertValues, tool output, and model output are untrusted data.
- Endpoint URLs originate only from validated startup settings.
- The model cannot choose a tool name or endpoint.
- The model cannot expand scope or time windows.
- All MCP capabilities are endpoint-bound and read-only.
- All arguments are validated immediately before network access.
- Evidence is committed before an agent can cite it.
- Agent chunk reads are run- and specialist-scoped.
- Root RCA synthesis has `tools=[]`.
- Specialist agents receive no mutation tool.
- Prompt injection cannot alter deterministic routing or capability resolution.
- Persisted analysis cites evidence references rather than copying raw payloads.

## 15. Testing Strategy

### Unit tests

- Skill registry loads canonical specialist capabilities.
- Capability discovery uses Skill requirements rather than every manifest entry.
- Each ADK specialist receives only its matching Skill.
- Each agent receives only its matching run-scoped evidence tools.
- `collect_evidence()` ignores model-controlled scope, endpoint, query, and tool names because none are accepted.
- Evidence commit occurs before the first chunk is returned.
- A repeated collection call is idempotent and causes no second MCP call.
- Cross-run and cross-specialist chunk access is rejected.
- Oversized responses fail closed.
- Chunk count and chunk size are bounded and deterministic.
- Trace chunks preserve normalized waterfall redaction.
- Specialist observations reject unknown or cross-specialist evidence references.
- Prompt injection in alert or telemetry cannot alter tools or routing.
- Corrective retries use stable codes and allowed references only.

### Workflow tests

- Selected specialist ADK agents start concurrently.
- Missing capabilities skip only the matching specialist.
- One failed specialist preserves the other analyses and yields `PARTIAL`.
- All routed specialists failing yields `FAILED`.
- Unsafe GCP and AWS scopes invoke neither MCP nor specialist agents.
- Deadline expiry cancels pending calls and prevents new tool calls.
- Root RCA prompt receives concrete specialist observations, not raw telemetry or generic availability summaries.

### Integration tests

- MCP discovery, collection, evidence persistence, analysis persistence, and RCA report persistence complete in one job lifecycle.
- A crash after evidence commit reuses persisted evidence rather than repeating MCP collection.
- Database constraints reject malformed analysis audit data.
- Specialist observation references resolve to exact persisted evidence partitions.
- Existing Pub/Sub idempotency and lease recovery remain valid.

### Evaluation tests

- Metrics anomaly, Trace critical path, and Log exception cases require concrete observations and citations.
- Large telemetry fixtures stay within configured model-input budgets and return `PARTIAL` when truncated.
- Conflicting specialist evidence reaches the root RCA agent with `SUPPORTS` and `CONTRADICTS` relations intact.
- Prompt injection fixtures cannot cause endpoint changes, mutation, invented evidence, or unsupported root-cause claims.

## 16. Rollout

1. Add contracts, persistence migration, and run-scoped evidence tools behind a disabled configuration flag.
2. Add specialist ADK adapters and workflow tests.
3. Run shadow analysis: preserve the existing report path while storing specialist analyses for comparison.
4. Evaluate latency, token usage, observation quality, citation validity, payload truncation, and failure rates.
5. Enable specialist analyses as root RCA input in non-production.
6. Enable in production after migration, evaluation thresholds, and rollback checks pass.

Rollback disables specialist analysis input and returns to the existing evidence-summary RCA path. Persisted evidence and optional analysis JSON remain readable; no destructive downgrade is required during application rollback.

## 17. Acceptance Criteria

- Metrics, Trace, and Log each execute as a Google ADK `LlmAgent` with the matching Skill.
- No specialist or root agent receives a raw `McpToolset`.
- Every specialist can invoke only its run-scoped, endpoint-isolated evidence tools.
- Evidence is persisted before it is returned to or cited by an agent.
- Every specialist observation cites known evidence belonging to that specialist.
- The RCA agent receives concrete validated observations and no raw telemetry.
- Large telemetry is bounded by configured byte, chunk, and tool-call limits.
- Partial failures preserve successful evidence and analyses.
- Existing no-safe-scope and AWS paths make zero MCP and model calls and remain honest `PARTIAL` reports.
- All unit, integration, contract, evaluation, lint, and type checks pass.
