# SRE Agent Frontend

SRE Agent Web 介面的獨立 Angular 22 standalone 應用程式。

## 前置需求

- Node.js `>=24.15.0 <25`
- npm 11

## 執行期設定

應用程式會在 Angular 啟動前取得 `/config.json`。部署環境必須提供以下三個欄位：

```json
{
  "apiBaseUrl": "/api/v1",
  "locale": "zh-TW",
  "timeZone": "Asia/Taipei"
}
```

若 response 無法取得或格式無效，啟動就會失敗。功能程式碼應注入
`RUNTIME_CONFIG`，不得寫死 API base URL。

## 常用命令

```bash
npm start
npm test -- --watch=false
npm run build
```

正式環境建置產物會寫入 `dist/frontend/`。

## 正式環境 Container

從儲存庫根目錄建置正式環境 image：

```bash
docker build -t sre-agent-frontend:gke-plan frontend
```

image 使用 unprivileged Nginx runtime 並監聽 port `8080`。它提供 `/healthz`、
針對 client-side route 回傳 Angular shell，而且絕不代理 `/api`；API endpoint 必須
透過部署的 `config.json` 提供。

若 root filesystem 設為唯讀，請將以下路徑掛載為可寫入 volume：

```text
/tmp
/var/cache/nginx
/var/run
```

`index.html` 與 `config.json` 不會被快取。只有包含 content hash 的 JavaScript、
CSS 與字型建置產物會收到一年期限的 immutable cache header。

## 操作方式與範圍

首頁列出 Incident 的 provider、專案／系統代碼（Grafana `folder`）、alert name、
嚴重度、狀態與 RCA 狀態。點選編號可查看原樣保留的 `AlertValues`、正規化提醒及
RCA 報告。按「重新整理」才會重新呼叫 REST API；本階段沒有 timer、SSE、
WebSocket、Chat 或 `sre-chat-backend`。

前端只使用 `apiBaseUrl` 下的 Operator API，不匯入 Backend/Worker source，也不直接
連線 PostgreSQL。所有介面文字與錯誤使用繁體中文；原始 labels、AlertValues 與
技術識別值維持原文。
