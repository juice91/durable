# 本方案 系統規格文件

> 版本：2026-11-18

## 目的、範圍與讀者

本文件規定本方案（Durable）的功能與非功能需求，作為開發、測試與驗收的依據。本方案是一套可自架的平台，用來建立、執行與觀測 AI Agent 及其工作流；每一次執行都是資料庫中的一筆持久紀錄，行程中斷不會遺失執行、也不會重做外部動作。

範圍涵蓋目前已合併至 main 的能力（2026-11-17，Studio 對等第二輪已部署）：Agent 與版本管理、工具與 MCP、工作流與人工審批、知識庫與記憶、對話、觀測、Studio 操作介面、多租戶與安全。規劃中但尚未合併的項目（附件儲存第二輪、MCP 本地執行環境與目錄、Architect Agent、資料集與評測）列於文末，不作為本版驗收依據。

讀者：平台工程師（實作與驗收）、產品與專案負責人（範圔確認）、導入團隊（理解系統能做什麼、不能做什麼）。架構決策與理由見另一份《架構設計文件》；開發方法見《AI 輔助開發流程方法》。

## 名詞定義與角色

| 名詞 | 定義 |
| --- | --- |
| 租戶（Tenant） | 資料與權限的最外層隔離單位；所有查詢都以呼叫者的租戶過濾，跨租戶的 id 一律回 404。 |
| Agent | 一份宣告式規格：系統提示、模型、工具清單、處理器、知識庫與記憶設定。有草稿與不可修改的已發布版本。 |
| 版本（Version） | Agent 或工作流發布後的不可變快照；可比較、可還原。執行永遠綁定某一版本或草稿快照。 |
| 執行（Run） | 一次 Agent、工具、對話回合或工作流的執行紀錄；一個執行對應一個持久工作流，id 相同。 |
| 事件（Event） | 執行過程中依序編號（seq）寫入的紀錄：模型請求、工具呼叫、文字片段、終態等。每個事件有唯一 event\_key，重放時回傳既有列。 |
| 步驟（Step） | 持久執行引擎記錄的最小可重放單位；輸出一旦寫入，重放時不再執行。 |
| 副作用（Side effect） | 對外部系統造成不可逆影響的呼叫；以「預留、呼叫、完成」三段記錄，絕不重做。 |
| 工具（Tool） | Agent 可呼叫的函式：平台內建工具或 MCP 伺服器提供的工具。 |
| MCP 伺服器 | 租戶註冊的外部工具伺服器（Streamable HTTP），經政策檢查的用戶端連線；標頭秘密加密保存。 |
| 工作流（Workflow） | 宣告式流程圖：Agent 步驟、工具呼叫、條件分支、平行分支、人工審批。分支條件是資料比對，不是程式碼。 |
| 審批（Approval） | 工作流中等待人員決定的節點；同意、拒絕、超時三者只有一個生效。 |
| 知識庫（Knowledge base） | 租戶上傳的文件集合，經抽取、切塊、嵌入後供檢索；檢索結果帶引用。 |
| 記憶（Memory） | 跨回合保留的資訊：工作記憶（結構化欄位）、觀察記憶（對話摘要）、語意回想（向量檢索歷史訊息）。 |
| 對話（Conversation） | 一串有歷史的 Agent 執行；同一對話同時只允許一個進行中的回合。 |
| 處理器（Processor） | 掛在 Agent 上的宣告式防護：PII 遮罩、正規表達式封鎖、長度限制、內容審核。 |
| Studio | 操作介面（Next.js）：編輯與發布 Agent、Playground、工作流圖、追蹤、日誌、指標。 |
| 控制層（API） | FastAPI 服務：驗證、租戶授權、記錄執行、排入佇列、事件串流。不執行任何 Agent 邏輯。 |
| 執行層（Worker） | 承載持久執行引擎與 Agent 執行環境的行程；從佇列取出執行並逐步落地。 |

角色：**平台管理者**管理模型與嵌入模型允許清單、租戶、部署；**租戶管理者**註冊 MCP 伺服器、管理知識庫與成員；**Agent 設計者**在 Studio 編輯、測試、發布 Agent 與工作流；**審批者**在 Studio 對等待中的節點做決定；**終端使用者**透過對話或外部前端與 Agent 互動；**外部系統**透過 API 以租戶金鑰啟動執行並訂閱事件串流。

## 功能需求

以下依程式碼盤點（分支頂端 8afde89，2026-11-14），每條需求標注對應的 API 或模組。全部 API 挂在 `/api/v1`，`/healthz` 除外。

### FR-1 Agent 與版本

| 編號 | 需求 | 依據 |
| --- | --- | --- |
| FR-1.1 | 建立、列出、讀取、更新 Agent 草稿：系統提示、模型、工具清單、處理器、知識庫、記憶、請求上下文欄位 | `POST/GET/PATCH /agents` |
| FR-1.2 | 發布為不可修改的版本；發布時檢查模型允許清單、MCP 伺服器存在、RE2 模式合法、提示佔位符字符 | `POST /agents/{id}/versions` |
| FR-1.3 | 比較任兩個版本的差異；從舊版本還原為新版本並記錄来源 | `GET /versions/compare`、`POST /versions/{n}/restore`，`restored_from` 欄 |
| FR-1.4 | 列出允許的模型；執行時可指定模型覆寫、版本或草稿快照 | `GET /models`，`runs.draft_spec` |
| FR-1.5 | Agent 宣告請求上下文欄位；執行時驗證客戶端傳入的上下文；`tenant_id`、`user_id`、`run_id` 為保留字，不接受客戶端輸入 | `GET /agents/{id}/request-context`、`POST …/validate` |

### FR-2 執行與事件串流

| 編號 | 需求 | 依據 |
| --- | --- | --- |
| FR-2.1 | 啟動 Agent 執行；可帶 `run_id` 做冪等重試，重複啟動回 409 | `POST /agents/{id}/runs` |
| FR-2.2 | 列出與讀取執行；狀態為 queued / running / recovering / completed / failed / needs\_attention | `GET /runs`、`GET /runs/{id}` |
| FR-2.3 | 重放持久事件日誌；事件 `seq` 連續無空洞，`event_key` 唯一 | `GET /runs/{id}/events?after_seq=` |
| FR-2.4 | SSE 即時串流：先重放 `Last-Event-ID` 或 `after_seq` 之後的事件，再送即時事件與 token 增量；每 10 秒心跳；非整數 `Last-Event-ID` 回 400 | `GET /runs/{id}/stream` |
| FR-2.5 | 每次模型請求記錄 token 與計價成本，與事件同一交易寫入；執行列帶總量 | `run_model_requests`，`config/model-prices.json` |
| FR-2.6 | 每次執行可取得 span 樹（含 token 與成本），由持久事件重建 | `GET /runs/{id}/trace` |
| FR-2.7 | 工具可單獨測試執行，沿用同一套允許清單與副作用規則 | `POST /tools/{id}/runs` → `eap.tool_run` |

### FR-3 對話

| 編號 | 需求 | 依據 |
| --- | --- | --- |
| FR-3.1 | 建立、列出、讀取、更新、刪除對話；對話綁定一個 Agent 與一個 resource（使用者） | `/conversations` |
| FR-3.2 | 在對話中啟動一個回合；同一對話同時只有一個進行中的回合，在對話列鎖下強制 | `POST /conversations/{id}/runs` |
| FR-3.3 | 歷史裁剪：預設最近 20 回合（1–200），可設 token 上限；歷史總量上限 1,000,000 bytes | `HistoryConfig`，`EAP_MAX_HISTORY_BYTES` |

### FR-4 工具與 MCP

| 編號 | 需求 | 依據 |
| --- | --- | --- |
| FR-4.1 | 列出工具目錄：內建工具（`clock.now`、示範副作用工具 `tickets.create`）與 Agent 使用中的 MCP 工具 | `GET /tools` |
| FR-4.2 | 平台工具由 Worker 自動注入，不列在 Agent 規格：`retrieve`（有知識庫時）、`update_working_memory`（有工作記憶時）；兩者為保留名稱 | `platform_tools.py`、`memory.py` |
| FR-4.3 | 租戶註冊 MCP 伺服器（Streamable HTTP）：建立、列出、讀取、更新、刪除、連線測試；標頭秘密 AES-GCM 加密、支持金鑰輪替、永不回傳 | `/mcp-servers`，`mcp_server_secrets`，`EAP_SECRETS_KEY` / `EAP_SECRETS_OLD_KEYS` |
| FR-4.4 | Agent 執行時可呼叫已註冊伺服器的工具；呼叫超時 60 秒；工具允許清單 `EAP_ALLOWED_TOOLS` | `mcp.py`，`EAP_MCP_TIMEOUT_SECONDS` |

### FR-5 工作流與審批

| 編號 | 需求 | 依據 |
| --- | --- | --- |
| FR-5.1 | 建立、列出、讀取、更新工作流草稿；發布為版本 | `/workflows`、`/workflows/{id}/versions` |
| FR-5.2 | 節點種類：Agent 步驟、工具呼叫、條件分支、平行分支、人工審批；分支條件是資料比對，不是程式碼 | `eap_schemas` 工作流規格 |
| FR-5.3 | 啟動工作流執行；Agent 步驟以子執行跑，平行分支以子工作流跑 | `POST /workflows/{id}/runs` → `eap.workflow_run`、`eap.workflow_branch` |
| FR-5.4 | 審批節點：同意、拒絕、超時三者競爭同一鍵，恰好一個生效；決定事件在執行列鎖下寫入 | `POST …/steps/{step}/approve` / `reject` |
| FR-5.5 | 上限：單步輸出 32 KiB，流程上下文 1 MiB，單次執行最長 14 天；審批等待每小時重檢一次 | `EAP_WORKFLOW_MAX_*`、`EAP_APPROVAL_POLL_SECONDS` |

### FR-6 知識庫

| 編號 | 需求 | 依據 |
| --- | --- | --- |
| FR-6.1 | 建立、列出、讀取（含嵌入花費）、更新、刪除知識庫；列出允許的嵌入模型 | `/knowledge-bases`、`GET /embedding-models` |
| FR-6.2 | 上傳、讀取、刪除、重新導入單一文件（Markdown、HTML、純文字）；單文件上限 5 MiB，單次導入 2,000,000 tokens | `PUT …/documents/{key}`，`EAP_KB_MAX_*` |
| FR-6.3 | 導入是持久工作流：抽取與切塊為純函式，嵌入批次逐批落地，中斷後從未完成批次繼續；每 Worker 同時 2 個導入 | `eap.kb_ingest`，`eap_knowledge`，`EAP_KB_INGEST_CONCURRENCY` |
| FR-6.4 | 整庫重建索引（新世代）與放棄待定世代 | `POST / DELETE …/reindex` |
| FR-6.5 | 搜尋：pgvector 餘弦距離，HNSW 索引；Agent 透過 `retrieve` 工具檢索，結果上限 24 KiB，引用編號跨整次執行連續 | `POST …/search`，`kb_chunks` |

### FR-7 記憶

| 編號 | 需求 | 依據 |
| --- | --- | --- |
| FR-7.1 | 工作記憶：範圍為 resource（預設）或 conversation；模板模式（Markdown 筆記 ≤ 16 KiB）或 schema 模式（JSON，深度 3，50 屬性）；可唯讀；重放時寫入恰好一次 | `working_memories`，`/memory/working`（ETag） |
| FR-7.2 | 觀察記憶：回合結束後由子工作流產生觀察與反思，每對話串行；參數 `observe_after_tokens` 12,000、`keep_recent_turns` 4、`reflect_after_tokens` 8,000 | `eap.memory_observe_run`，`memory_observations` |
| FR-7.3 | 語意回想：回合前檢索歷史訊息（`top_k` 4，上限 10；前後文 1，上限 3），8 秒超時一次重試，結果 ≤ 8 KiB；回合後嵌入新訊息 | `message_embeddings`，`eap.memory_recall` / `eap.memory_index` |
| FR-7.4 | 遺忘：刪除對話的觀察、單一使用者在一個 Agent 或全租戶的記憶 | `DELETE /memory/resources/{id}` |
| FR-7.5 | 保留期掃除：排程工作流每日 03:00 UTC，依類別設天數，批次 500；截止時間以排程時間計算，恢復後決定一致 | `eap.memory_retention`，`EAP_MEMORY_RETENTION_*` |

### FR-8 處理器（防護）

| 編號 | 需求 | 依據 |
| --- | --- | --- |
| FR-8.1 | Agent 可在輸入與輸出各掛最多 8 個處理器；動作為 allow / redact / block / truncate | `eap_schemas.processors` |
| FR-8.2 | 種類：`pii_redact`（email、電話、信用卡、IBAN、IPv4、US SSN）、`regex_blocklist`（≤ 20 模式，每模式 ≤ 200 字元，RE2）、`max_length`（≤ 1,000,000 字元）、`moderation`（允許清單模型，只能 block） | `eap_processors` |
| FR-8.3 | 列出處理器目錄與設定 schema；測試處理器；查詢處理結果；匹配到的文字不儲存 | `GET /processors`、`POST /processors/test`、`GET /processor-results` |

### FR-9 觀測

| 編號 | 需求 | 依據 |
| --- | --- | --- |
| FR-9.1 | 指標：摘要、時間序列、分類（執行數、token、成本、延遲，含 RAG 與記憶） | `GET /metrics/summary` / `timeseries` / `breakdown` |
| FR-9.2 | 日誌搜尋：查 Loki，以租戶標籤限定，不暴露其他租戶 | `GET /logs`，`EAP_LOKI_URL` |
| FR-9.3 | 每次執行一條 OTel 追蹤，從 API 請求到每個工具呼叫；輸出前遮罩 | `eap_observability` |

### FR-10 Studio

路由與用途：`/agents`（列表）、`/agents/new`（建立步驟）、`/agents/{id}/chat`（對話，含 Playground 執行選項）、`/agents/{id}/editor`（分步編輯器，含知識與記憶步驟、版本比較）、`/agents/{id}/memory`（記憶檢視）、`/agents/{id}/traces`；`/workflows`、`/workflows/{id}`（圖與執行）、`/workflows/{id}/runs/{runId}`（即時圖）；`/tools`、`/mcp`、`/knowledge`、`/processors`；`/traces`（追蹤抽屜）、`/metrics`（預設首頁）、`/logs`、`/settings`。舊路由 `/playground`、`/runs/{id}`、`/traces/{runId}` 導向新頁。

### FR-11 多租戶與驗證

| 編號 | 需求 | 依據 / 狀態 |
| --- | --- | --- |
| FR-11.1 | 所有資料與查詢以租戶隔離；子列以複合外鍵綁租戶；其他租戶的 id 回 404 | 全部 store，已实作 |
| FR-11.2 | 驗證模式 `dev`（本地）與 `api_key`（租戶金鑰，scope `mcp` / `manage` / `otlp`） | `EAP_AUTH_MODE`，已实作 |
| FR-11.3 | 企業帳號登入：SSO、MFA、密碼重設；企業 IdP 以 SAML / OIDC 接入；Studio 與外部前端走 OIDC PKCE | 規劃：`EAP_AUTH_MODE=oidc`，身分供應者 ZITADEL |
| FR-11.4 | 使用者永遠屬於一個租戶；租戶由身分供應者的組織（Organization）对應，第一次登入時自動建立租戶列 | 規劃 |
| FR-11.5 | 角色：管理者、設計者、審批者、檢視者，全租戶範圍；由租戶管理者在身分供應者指派，進入 token；API 依角色檢查寫入與審批 | 規劃：Project roles，`require_role` |
| FR-11.6 | 機器存取（外部前端、MCP、OTLP）以服務帳號與 PAT / JWT 金鑰，scope 用同一套角色；與現有租戶金鑰並收一個版本 | 規劃 |
| FR-11.7 | 使用者停用或刪除時，其記憶與資源走既有的遺忘 API | 規劃：身分供應者事件 → `DELETE /memory/resources/{rid}` |
| FR-11.8 | 資源級授權（某 Agent 或工作流只給特定人）存於平台資料庫，與資源同交易寫入；不外托身分供應者 | 規劃：`resource_grants` 表；OpenFGA 待需求再評 |

## 非功能需求

| 編號 | 需求 | 驗證方式 |
| --- | --- | --- |
| NFR-1 持久性 | 每次執行是一個 DBOS 工作流，id 等於 run id。Worker 在任一步驟被 `kill -9` 後，同 executor id 的新 Worker 必須接手并完成，已記錄的步驟不重執行 | `tests/recovery` 崩潰矩陣，19 個具名崩潰點 |
| NFR-2 副作用恰好一次 | 外部副作用以 `run_id:tool_call_id` 預留；中斷在呼叫與完成之間時執行結束在 `needs_attention`，永不自動再呼叫 | 假外部服務以冪等鍵計數，每情境斷言恰好 1 |
| NFR-3 事件完整性 | `seq` 在列鎖下配號，連續、無重複；重放的 append 回傳既有列；SSE 重放與資料庫一致 | 崩潰矩陣斷言；API 套件 |
| NFR-4 決定性重放 | 時鐘、版本解析、環境上限、審批期限在被記錄的步驟裡讀一次；步驟內的例外可 pickle，重放時型別與訊息不變 | Worker 套件 |
| NFR-5 租戶隔離 | 跨租戶讀取一律 404；日誌搜尋不跨租戶；保留上下文欄位不接受客戶端輸入 | API 套件的跨租戶情境（每任務必測） |
| NFR-6 輸入防護 | 請求體 > 256 KiB 回 413（文件上傳 5 MiB）；非嚴格 UTF-8 或含 NUL 回 415；JSON 深度 > 32 或不可儲存字串回 422，最多報 20 條，不回顯輸入；401 先於 422 | `request_guard.py` 測試 |
| NFR-7 外連政策 | MCP 流量僅 https；拒絕私有、回錄、link-local、metadata 位址；連線時重檢 DNS；不跟隨重導；回應上限 1 MiB | `eap_secrets` 套件，`test_mcp_safety.py` |
| NFR-8 秘密 | MCP 標頭值 AES-GCM 加密，支持舊金鑰輪替，永不回傳；工具結果、錯誤、日誌、span 遮罩秘密回顯；格式錯誤的金鑰讓啟動失敗 | `eap_secrets` 套件 |
| NFR-9 模型治理 | 模型與嵌入模型允許清單在發布、執行開始、每次呼叫前檢查；測試不呼叫真実模型（`test:*` 腳本模型） | `EAP_ALLOWED_MODELS`、`EAP_ALLOWED_EMBEDDING_MODELS` |
| NFR-10 延遲與容量 | 佇列輪詢 1 秒；心跳 5 秒，20 秒無心跳標為 recovering；SSE 串流上限 200 全局 / 10 每主體，實命 3600 秒；嵌入批次 32 輸入 / 16,384 tokens，超時 120 秒 | 設定值；非正式效能測試 |
| NFR-11 可觀測性 | 每次執行一條追蹤；日誌帶租戶與執行 id；用量與成本與事件同交易；只有 `eap_observability` import OpenTelemetry | import-linter；觀測套件 |
| NFR-12 Schema 管理 | Alembic 是唯一 schema 所有者；API 與 Worker 在非 head 時拒絕啟動；每個遷移有 downgrade 與測試，`alembic check` 保持乾淨 | `packages/db/db_tests` |
| NFR-13 層級邊界 | 9 条 import-linter 契約（見《架構設計文件》），違反則建置失敗 | `uv run lint-imports` |
| NFR-14 可存取性（Studio） | Lighthouse a11y ≥ 95；400 px 無水平捲動；純鍵盤可操作；支持 `prefers-reduced-motion` | SL 驗收清單，axe 測試 |
| NFR-15 授權與品牌 | 不包含第三方產品的程式碼、名稱、標誌；第三方套件列於 `THIRD_PARTY_NOTICES.md` | 審查者檢查；品牌字 grep |

## 介面規格

所有路由挂在 `/api/v1`，JSON 請求與回應；讀取他租戶資源回 404；請求守衛在路由之前執行（見 NFR-6）。

| 資源 | 端點 | 說明 |
| --- | --- | --- |
| Agent | `POST/GET /agents`、`GET/PATCH /agents/{id}` | 草稿的 CRUD |
| 版本 | `POST/GET /agents/{id}/versions`、`GET …/versions/{n}`、`GET …/versions/compare`、`POST …/versions/{n}/restore` | 發布、比較、還原 |
| 執行選項 | `GET /models`、`GET /agents/{id}/request-context`、`POST …/request-context/validate` | 允許模型、上下文欄位 |
| 執行 | `POST /agents/{id}/runs`、`GET /runs`、`GET /runs/{id}`、`GET /runs/{id}/events`、`GET /runs/{id}/stream`、`GET /runs/{id}/trace` | 啟動 201，重複 409；無取消端點 |
| 對話 | `POST /agents/{id}/conversations`、`GET /conversations`、`GET/PATCH/DELETE /conversations/{id}`、`POST /conversations/{id}/runs` | 回合走 `eap.agent_run` |
| 工具 | `GET /tools`、`GET /tools/{id}`、`POST /tools/{id}/runs` | 單工具測試執行 |
| MCP | `POST/GET /mcp-servers`、`GET/PATCH/DELETE /mcp-servers/{id}`、`POST /mcp-servers/{id}/test` | 秘密不回傳 |
| 工作流 | `POST/GET /workflows`、`GET/PATCH /workflows/{id}`、`POST/GET …/versions`、`GET …/versions/{n}` |  |
| 工作流執行 | `POST /workflows/{id}/runs`、`POST /workflows/runs/{run}/steps/{step}/approve`、`…/reject` | 審批決定 |
| 知識庫 | `GET /embedding-models`、`GET/POST /knowledge-bases`、`GET/PATCH/DELETE /knowledge-bases/{id}`、`GET …/documents`、`GET/PUT/DELETE …/documents/{key}`、`POST …/documents/{key}/reingest`、`POST/DELETE …/reindex`、`POST …/search` | PUT 上傳上限 5 MiB |
| 記憶 | `GET/PUT/DELETE /conversations/{id}/memory/working`（ETag）、`GET /agents/{id}/memory/working`、`GET/PUT/DELETE /agents/{id}/memory/resources/{rid}/working`、`GET/DELETE /conversations/{id}/memory/observations`、`DELETE …/observations/{oid}`、`DELETE /agents/{id}/memory/resources/{rid}`、`DELETE /memory/resources/{rid}` | 最後兩條為遺忘 |
| 處理器 | `GET /processors`、`POST /processors/test`、`GET /processor-results` |  |
| 指標與日誌 | `GET /metrics/summary`、`/metrics/timeseries`、`/metrics/breakdown`、`GET /logs` | 日誌查 Loki |
| 健康 | `GET /healthz` | 無驗證 |

### 事件串流（SSE）

`GET /runs/{id}/stream` 回 `text/event-stream`。客戶端以 `Last-Event-ID` 標頭或 `?after_seq=` 指定已收到的最後序號（標頭優先，預設 -1）；伺服器先重放之後的持久事件，再送即時事件與 token 增量；每 10 秒送 `: heartbeat` 註解；`end` 事件關閉串流。持久事件與暫時增量經 Postgres `NOTIFY` 從 Worker 到 API。

事件種類（`event_key` 形式）：`start`、`model:{n}:final`、`tool:{call_id}:call`、`tool:{call_id}:result`、處理器結果、工作流步驟開始與結束、審批決定、`end`。

### 工作流與佇列（內部）

| DBOS 工作流 | 觸發 | 佇列 |
| --- | --- | --- |
| `eap.agent_run` | Agent 執行、對話回合、工作流的 Agent 步驟 | `eap-agent-runs` |
| `eap.tool_run` | 單工具測試 | `eap-agent-runs` |
| `eap.workflow_run`、`eap.workflow_branch` | 工作流執行與平行分支 | `eap-agent-runs` |
| `eap.kb_ingest` | 文件上傳、重新導入、重建索引 | `eap-kb-ingest`（每 Worker 2） |
| `eap.memory_observe_run` | 回合結束後 | `eap-memory-observe`（每對話 1） |
| `eap.memory_retention` | 排程 `0 3 * * *` UTC | 排程工作流 |

## 資料模型摘要

兩個資料庫，都在同一個 Postgres：`eap_app`（產品資料，Alembic 管理）與 `eap_dbos`（DBOS 系統表：工作流狀態、步驟輸出、佇列）。所有產品表都帶 `tenant_id`，子表以複合外鍵綁租戶。

| 遷移 | 新增表 | 其他變更 |
| --- | --- | --- |
| 0001\_baseline | `agents`、`agent_versions`、`runs`、`execution_events`、`side_effects`、`fake_external_tickets`、`fake_external_ticket_calls` |  |
| 0002\_conversations | `conversations` | `runs` 加 `conversation_id`、`turn`、`messages` |
| c689c50ff203 |  | `runs` 加 `trace_id`、`traceparent` |
| 0003 |  | `runs` 租戶與建立時間索引 |
| 0004\_mcp\_servers | `mcp_servers`、`mcp_server_secrets` |  |
| 0005\_workflows | `workflows`、`workflow_versions` |  |
| 0006\_tool\_runs |  | `runs` 加 `kind`、`tool_id` |
| 0007\_workflow\_runs |  | `runs` 加 `parent_run_id`、`parent_step_id`、`workflow_id`、`workflow_version` |
| 0008\_run\_usage | `run_model_requests` | `runs` 加 token、成本、模型、時間欄 |
| 0009 |  | `agent_versions.restored_from` |
| 0010\_run\_options |  | `runs` 加 `draft_spec`、`request_context` |
| 0011\_knowledge\_bases | `knowledge_bases`、`kb_documents`、`kb_chunks`（pgvector，HNSW cosine） | `runs.knowledge_base_id` |
| 0012\_memory | `working_memories`、`memory_observations` | `conversations.resource_id` |
| 0013\_semantic\_recall | `message_embeddings`（pgvector）、`memory_forget_watermarks` |  |

核心關係：`agents` 1—n `agent_versions`；`agents` 1—n `runs`（執行綁定版本號或 `draft_spec`）；`runs` 1—n `execution_events`（`seq` 連續，`event_key` 唯一）；`runs` 1—n `run_model_requests`；`runs` 1—n `side_effects`（key = `run_id:tool_call_id`）；`runs` 自己關聯自己（`parent_run_id`，工作流的子執行）；`conversations` 1—n `runs`（`turn`）；`knowledge_bases` 1—n `kb_documents` 1—n `kb_chunks`；`conversations` / resource 1—n `working_memories`、`memory_observations`、`message_embeddings`。

`runs.status` 的狀態機：`queued` → `running` → （`recovering`，心跳超過 20 秒）→ `completed` | `failed` | `needs_attention`。終態帶 `finished_at` 與用量總量。main 上其後的遷移（0016、0017：API 金鑰、AG-UI）未列入本表。

## 驗收標準

以下全部通過才算本版驗收；前三項由自動化判定，後三項由人在開發棧上走一次。

1. `bash scripts/test.sh` 通過，無 skip：import-linter、全部 pytest 套件（含 `tests/recovery`）、API 套件對真 Postgres、Studio vitest、開發棧生命週期腳本、pgvector 升級腳本。
2. 崩潰矩陣的每個情境：副作用恰好一次；`seq` 連續無重複；執行結束在 `completed` 且 DBOS 工作流恰一個 SUCCESS；SSE 重放與資料庫一致；每次模型請求一列用量。
3. CI（hosted）與 CI Heavy（自架）在 main 上綠；每夜 heavy 含 worker 與 recovery 套件。
4. 瀏覽器檢查：建立 Agent → 發布 → 在 Playground 以 `test:ticket` 執行，看到兩次模型請求帶 token 與成本 → 追蹤抽屜顯示 span 樹 → 日誌搜尋找到該執行 → 指標頁統計增加。
5. 工作流檢查：建立含 Agent 步驟、分支、審批的流程 → 執行 → 在審批節點停下 → 重啟 Worker → 在 Studio 按同意 → 流程繼續到結束；決定事件只有一筆。
6. 知識與記憶檢查：上傳 Markdown 文件 → 導入完成 → Agent 以 `retrieve` 回答並帶引用 → 對話中更新工作記憶 → 記憶檢視頁顯示內容 → 刪除資源後記憶消失。
7. Studio：1440 / 1024 / 768 / 400 px、深淺色、純鍵盤都可操作；Lighthouse a11y ≥ 95；無品牌字。
8. 最終整支審查沒有未解的 Critical 或 Important。

## 範圍外與規劃中

| 項目 | 狀態（2026-11-19） | 說明 |
| --- | --- | --- |
| AG-UI 輸出轉接器、API key、CopilotKit 範例 | 已合併（遷移 0016、0017） | AG-UI 是從 durable event log 轉出的輸出層，不取代原生串流 |
| MCP runtime 與 catalog（ToolHive） | 已上線（PR 31、33） | 36 tools；scope `mcp` / `manage`；FR-4 只涵蓋外部註冊 |
| Attachments r2（R2 object storage） | 已合併 |  |
| Architect Agent | 已上線 | 對話設計 Agent 與 Workflow；draft 免審，publish 需 approval；platform.\* 讀取 tools（PR 45） |
| Evals：test suite、scorer、suggested rules | 已上線（PR 43、46） | 從 run 紀錄建立 eval；里程碑 4 的核心已交付 |
| Secret input、http.request tool、KB source sync、OTLP ingest | 已上線（PR 12、34、19/42、53） | 遷移至 0032 |
| Open-core split、docs site | 已合併（PR 55、48、50） | Public repo 準備中：audit PR 49 待 review |
| Identity 整合（AUTH） | 規劃，需求見 FR-11.3–11.8 | ZITADEL；平台只改 `current_principal` |
| Studio parity round 3 | 規劃中 |  |
| KB 原生文件格式（PDF、Office） | P1 後續 | 現在只有 Markdown、HTML、純文字 |
| Run cancel endpoint | 無 |  |
| Workflow loop node | 分支進行中 |  |

本文件的 FR 章節依 2026-11-14 的程式碼（8afde89）盤點；上表「已上線」的能力尚未展開為 FR，下一次更新時補。下一個 milestone（2026-11-19）分四條主線：A Identity 與 Governance（對應 FR-11.3–11.8，另加稽核表、配額與預算）、B Authoring 與 Verification（per-node 編輯、dry run、從第 N 步重跑、工具 mock、發佈門檻）、C Connect 與 Embed（webhook、通知連接器、OAuth、SDK、本機 runner）、D Operate（模型 fallback、告警、bundle、注入偵測、資料治理）。每項的驗收條件在該 milestone 計畫；交付後展開為 FR-12 以後的章節。

明確不在範圍：租戶自帶程式碼在 Worker 執行；跨資料庫部署（佇列或向量改用外部服務）；在請求線程內同步執行 Agent。
