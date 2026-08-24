# RCA 簡化 Trace 瀑布圖設計

## 目標

在 Incident 的 RCA 詳細頁內建簡化 Trace 瀑布圖，讓值班人員不離開系統即可看出代表 Trace 中各服務與操作的父子關係、開始時間、耗時，以及位於 critical path 的異常 Span。

第一版聚焦單一代表 Trace。系統自動選擇最能支持 RCA 根因的 Trace，不提供多 Trace 切換、縮放、搜尋、匯出或複雜篩選。

## 現況與限制

- RCA Worker 已能將 MCP 結果保存至 `evidence_records`，但 Trace evidence 尚未定義供 UI 使用的穩定 Span 結構。
- Operator API contract 已定義 evidence 摘要，刻意排除原始工具結果；目前 Backend 也尚未實作 Trace 瀑布圖的專用 read model 與 route。
- Frontend 目前只顯示 RCA 報告摘要、根因、影響與修復建議，沒有 Trace 元件。
- 原始 MCP 結果可能包含 token、HTTP body、SQL 參數、個資或 baggage，因此不能直接提供給前端。

## 核准方案

採用 Worker 正規化、Backend 授權與投影、Frontend 純呈現的分層方式：

```text
Trace MCP
  → RCA Worker 正規化與代表 Trace 選擇
  → evidence_records.structured_data
  → Backend Trace Waterfall read model
  → GET /api/v1/rca-runs/{id}/trace-waterfall
  → RCA 頁面 TraceWaterfallComponent
```

不採用下列方案：

- Backend 直接解析 provider-specific 原始 MCP 結果：會讓 Backend 耦合各種 Trace 格式。
- Frontend 直接查詢 Grafana 或 Tempo：會引入認證、CORS、權限與敏感資料外洩風險。

## Worker 正規化模型

Trace specialist 將 MCP 結果轉成 provider-neutral 結構，存入 Trace evidence 的 `structured_data`。正規化結果包含：

### Trace 欄位

- `schemaVersion`：第一版固定為 `1`。
- `traceId`：Trace 識別碼。
- `rootServiceName`：根服務名稱。
- `rootOperationName`：根操作名稱。
- `startedAt`：UTC 時間。
- `durationMs`：Trace 總耗時，非負數。
- `spanCount`：正規化前的總 Span 數。
- `representativeScore`：代表 Trace 選擇分數。
- `spans`：供瀑布圖顯示的 Span 陣列。
- `truncated`：是否因 100 Span 上限而截斷。

### Span 欄位

- `spanId`：Span 識別碼。
- `parentSpanId`：根 Span 為 `null`。
- `serviceName`：服務名稱。
- `operationName`：操作名稱。
- `startOffsetMs`：相對 Trace 起點的非負偏移。
- `durationMs`：Span 耗時，非負數。
- `status`：`OK`、`ERROR` 或 `UNSET`。
- `kind`：`INTERNAL`、`SERVER`、`CLIENT`、`PRODUCER` 或 `CONSUMER`。
- `criticalPath`：是否位於 critical path。
- `attributes`：經過固定允許清單過濾的字串、數字或布林值。

允許顯示的 attributes 僅包含判讀服務與操作所需欄位，例如 HTTP method/status、RPC system/service/method、DB system/operation、server address/port。token、authorization、cookie、HTTP body、SQL statement/parameters、個資及任意 baggage 一律排除。

### 代表 Trace 選擇

Worker 對同一 RCA 時間窗中的候選 Trace 使用可重現的排序：

1. 根 Span 或 critical path 是否包含錯誤。
2. 相對基準的延遲異常程度。
3. 是否包含 RCA 已引用的異常服務或操作。
4. 總耗時。
5. `traceId` 字典序作為穩定 tie-breaker。

最高分 Trace 標記為代表 Trace。第一版 API 只回傳該筆 Trace。

### Span 截斷

第一版最多顯示 100 個 Span。超過上限時依序保留：

1. critical path Span。
2. 錯誤 Span。
3. 上述 Span 的祖先節點。
4. 其餘 Span 按開始時間填滿上限。

回應將 `truncated` 設為 `true`，前端顯示截斷提示。若保留必要祖先會超過上限，保留完整 critical path 與其祖先，並捨棄其他分支。

## Backend API

新增：

```http
GET /api/v1/rca-runs/{id}/trace-waterfall
```

回應物件固定包含 `trace`：

- RCA run 存在且有有效 Trace evidence：`trace` 為正規化 Trace。
- RCA run 存在但無 Trace evidence，或 evidence 無法安全正規化：`trace` 為 `null`。
- RCA run 不存在或呼叫者沒有 Incident 權限：沿用既有不可區分的 `404 RESOURCE_NOT_FOUND`。

Backend 透過 `rca_runs → incidents` 套用與 RCA report 相同的授權條件，只讀取 `evidence_records.structured_data`，不讀取或回傳 `raw_result`。Repository 會驗證 `schemaVersion`、必要欄位、數值範圍、父節點引用與 attributes 型別；單筆格式錯誤不影響 RCA 報告，只會使 `trace` 為 `null`。

OpenAPI 新增 `TraceWaterfallResponse`、`TraceWaterfall`、`TraceWaterfallSpan` schema，所有回應欄位採 camelCase 且 `additionalProperties: false`。Backend 的 Python read model 使用 snake_case，交由既有 schema alias 規則轉換。

## Frontend 畫面

新增獨立的 `TraceWaterfallComponent`，放在 RCA 報告的「影響」下方與「修復建議」上方。

### 顯示內容

- 頂部：根服務、根操作、縮短顯示的 Trace ID、總耗時與 Span 數量。
- 左側：依 `parentSpanId` 建立的服務／操作縮排樹。
- 右側：以 Trace 總耗時為基準，按 `startOffsetMs` 和 `durationMs` 計算長條位置與寬度。
- critical path 或錯誤 Span：紅色高亮。
- 其他 Span：依服務名稱產生穩定配色。
- 截斷資料：顯示原始 Span 數量與目前顯示上限提示。

### 互動

- 預設選取排序後第一個異常 critical-path Span；若不存在則選根 Span。
- 點擊 Span 後，下方顯示服務、操作、耗時、狀態、類型與允許顯示的 attributes。
- 元件使用可聚焦的 button row，支援鍵盤操作及明確 focus 樣式。
- 小螢幕保留欄位寬度並允許水平捲動，不把時間軸壓縮到不可辨識。

### 載入與錯誤狀態

- 載入中：顯示區塊內骨架。
- `trace: null`：顯示「目前沒有可呈現的 Trace evidence」。
- API 失敗：顯示區塊內錯誤與「重新載入」按鈕；Incident 與 RCA 報告仍可正常使用。
- 前端只用 Angular 文字繫結渲染內容，不使用未過濾 HTML。

Incident 詳細頁在取得最新 RCA run 後，並行載入 report 與 trace-waterfall。瀑布圖失敗時不使 `forkJoin` 整體失敗；Trace request 在元件或 facade 層轉換成獨立的 loading/error/empty 狀態。

## 元件邊界

- Worker Trace normalizer：只負責 provider-neutral 轉換、敏感欄位過濾、代表 Trace 排序與 Span 截斷。
- Backend repository：只負責授權後讀取與安全投影，不解析 provider-specific raw payload。
- Backend router/schema：只負責 HTTP contract 與序列化。
- Frontend API client/models：只負責型別化傳輸。
- `TraceWaterfallComponent`：只負責樹狀排列、比例計算、選取狀態及呈現，不發出 HTTP request。
- Incident detail container：負責取得最新 run 並協調 report 與 waterfall 狀態。

## 本地示範資料

完成實作後，將目前本機 `INC-227` 的 Trace evidence 補為五個 Span：

1. `checkout-api / POST /checkout`，1,925ms。
2. `checkout-api / validate-cart`，120ms。
3. `inventory-service / reserve-items`，280ms。
4. `checkout-api / db.connection.acquire`，1,480ms，ERROR 且位於 critical path。
5. `postgres / INSERT orders`，95ms。

本地資料更新不屬於 production migration；它只用於驗證已核准的 UI 效果。

## 測試策略

### RCA Worker

- 正規化亂序 Span 並建立正確父子關係。
- 使用穩定 tie-breaker 選擇代表 Trace。
- 保留 critical path、錯誤 Span 與必要祖先後套用 100 Span 上限。
- 移除敏感 attributes，只保留允許清單。
- 拒絕負耗時、無效父節點與不支援的 schema 型別。

### Backend

- Contract test 驗證新 route、camelCase UTC 回應與 schema 欄位集合。
- Repository integration test 驗證 global access、scope grant、未授權與不存在資源不可區分。
- 驗證有 Trace、無 Trace、無效 schema、截斷結果與 `raw_result` 不會出現在回應。

### Frontend

- API client test 驗證 URL 編碼及 response model。
- 元件 test 驗證縮排、時間比例、穩定配色、異常高亮與預設選取。
- 元件互動 test 驗證點擊及鍵盤選擇後的詳細資訊。
- 驗證 loading、empty、error、retry 與 truncated 提示。
- Incident detail test 驗證 Trace API 失敗不影響 RCA report。

### 完整驗證

- 執行 Backend、RCA Worker 與 Frontend 的相關測試及完整建置。
- 啟動本地服務，以 `INC-227` 驗證五個 Span、錯誤高亮、點擊詳情與 responsive 水平捲動。
- 確認沒有未授權 attributes 或原始 MCP payload 出現在 API 與 DOM。

## 不在第一版範圍

- 多 Trace 選擇與分頁。
- 縮放、拖曳、搜尋、匯出與複雜 Span filter。
- 直接嵌入 Grafana 或 Tempo。
- 對外提供 raw Trace payload。
- 新增 RCA 資料表或 migration。
