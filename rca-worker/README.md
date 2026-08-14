# SRE RCA Worker

This package is the independently deployable Pub/Sub consumer and RCA
orchestrator. It shares only versioned contracts and the PostgreSQL application
role with the Backend; it never imports Backend source code.

## 本機執行

從 repository root 執行 `docker compose up -d postgres pubsub-emulator`，並設定
`PUBSUB_EMULATOR_HOST=127.0.0.1:58085` 與 `PUBSUB_PROJECT_ID=sre-agent-test`。
依序套用 Backend 與 Worker migrations，再啟動 outbox publisher 及
`sre-agent-rca-worker`。官方 Emulator 只供本機開發／整合測試；production 不設定
emulator host，Pub/Sub 使用 ADC／Workload Identity。

RCA 總期限 300 秒、claim lease 60 秒、最多 3 次。只有 terminal DB commit 後才
ack；暫時性失敗 nack。MCP 不傳 authentication material，只暴露通過 endpoint、
schema hash 與 read-only annotation 驗證的 capability。本套件不含 Chat、SSE、
WebSocket 或 `sre-chat-backend`，也不執行任何修復 mutation。
