# SRE Agent frontend

Independent Angular 22 standalone application for the SRE Agent web interface.

## Requirements

- Node.js `>=24.15.0 <25`
- npm 11

## Runtime configuration

The application fetches `/config.json` before Angular starts. Deployment must provide all three fields:

```json
{
  "apiBaseUrl": "/api/v1",
  "locale": "zh-TW",
  "timeZone": "Asia/Taipei"
}
```

Startup fails if the response is unavailable or invalid. Feature code should inject `RUNTIME_CONFIG` rather than hard-code an API base URL.

## Commands

```bash
npm start
npm test -- --watch=false
npm run build
```

Production build artifacts are written beneath `dist/frontend/`.

## 操作方式與範圍

首頁列出 Incident 的 provider、專案／系統代碼（Grafana `folder`）、alert name、
嚴重度、狀態與 RCA 狀態。點選編號可查看原樣保留的 `AlertValues`、正規化提醒及
RCA 報告。按「重新整理」才會重新呼叫 REST API；本階段沒有 timer、SSE、
WebSocket、Chat 或 `sre-chat-backend`。

前端只使用 `apiBaseUrl` 下的 Operator API，不匯入 Backend/Worker source，也不直接
連線 PostgreSQL。所有介面文字與錯誤使用繁體中文；原始 labels、AlertValues 與
技術識別值維持原文。
