# PostgreSQL Schema Reference 文件設計

## 目的

建立一份供開發者、維運人員與資料庫管理者閱讀的繁體中文資料庫參考文件，集中說明 SRE AI Agent 的 PostgreSQL 18 告警與 Incident schema，以及實際使用的 DDL 語法。

## 文件位置

實作文件固定放在 `docs/database/postgresql-schema.md`。

## 單一執行來源

Alembic migration 是建立與修改資料庫 schema 的唯一執行來源。參考文件中的 SQL 用於閱讀、審查與故障排查，不建立獨立的 `schema.sql`，也不要求使用者直接執行文件內的 SQL。

每次 Alembic migration 修改資料表、約束、索引或分割邏輯時，必須在同一個變更中同步更新參考文件。

## 文件內容

文件必須包含：

1. PostgreSQL 18 與 UTC `TIMESTAMPTZ` 等共同規則。
2. 每張資料表的用途、主要欄位與資料關係。
3. 完整且可讀的 `CREATE TABLE` 語法，包括 UUID、JSONB、主鍵與外鍵。
4. `CHECK`、`UNIQUE` 與 partial unique constraint／index 的語法和用途。
5. 查詢與工作流程所需的 B-tree index 語法。
6. 六張月分割表的 `PARTITION BY RANGE (partition_timestamp)` 定義。
7. 複合主鍵 `(id, partition_timestamp)`，以及參照分割資料時使用雙欄位外鍵的原因。
8. current／next month partition 與任意月份 partition 的 `CREATE TABLE ... PARTITION OF ... FOR VALUES FROM ... TO ...` 範例。
9. `ensure_monthly_partitions(connection, month)` 的行為、允許清單與 exclusive upper bound 規則。
10. 本機使用 `docker-compose.yml` 啟動 PostgreSQL 18，以及執行 Alembic migration 的指令。

## 結構

文件依下列順序編排：

1. 適用範圍與權威來源
2. 本機啟動與 migration 指令
3. 命名、型別與時間規則
4. Schema 關係總覽
5. 非分割資料表 DDL
6. 月分割資料表 DDL
7. 約束與索引
8. Partition 建立與維護
9. 驗證與故障排查查詢

長 SQL 依功能拆成多個 fenced code block，避免把整份 migration 原樣貼成一個難以閱讀的大區塊。

## 正確性與驗證

- 文件中的資料表、欄位、約束、索引名稱及 SQL 必須與目前 Alembic revision 一致。
- 文件不得出現 Cloud SQL 建置、Kubernetes 或正式環境 infrastructure 配置。
- 文件不得包含正式密碼或 secret。
- 審查時以 migration diff 與 PostgreSQL catalog 整合測試交叉確認文件內容。
- 新增一項輕量文件一致性檢查，驗證文件涵蓋所有 migration 建立的資料表與必要分割表；不以脆弱的逐字全文比對取代真實資料庫測試。

## 不在範圍內

- 不建立第二份可直接部署的 `schema.sql`。
- 不取代 Alembic upgrade／downgrade。
- 不說明 Cloud SQL、網路、IAM、備份或正式部署方式。
- 不修改 Angular 前端。
