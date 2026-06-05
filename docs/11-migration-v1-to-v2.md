# 11 v1 → v2 迁移指南

- **版本**：v2.0-draft
- **日期**：2026-04-17
- **读者**：已部署 v1（`xiaopaw-with-memory/`）并计划升级到 v2（`xiaopaw-v2/`）的运维 / 实施工程师
- **配套文档**：[DESIGN.md §14](../DESIGN.md) / [03-data.md §15](03-data.md) / [09-config.md](09-config.md) / [07-security.md](07-security.md)
- **前置假设**：v1 单节点部署、日活 session ≤ 1000、pgvector 单实例、AIO-Sandbox 单容器

本文提供一份**可执行**的升级 SOP。每节给出具体脚本、校验命令、回滚开关。

---

## 目录

1. [迁移策略总览](#1-迁移策略总览)
2. [Phase 0 预准备](#2-phase-0-预准备)
3. [数据兼容性矩阵](#3-数据兼容性矩阵)
4. [Schema 变更](#4-schema-变更)
5. [配置变更](#5-配置变更)
6. [代码变更对照表](#6-代码变更对照表)
7. [测试回归清单](#7-测试回归清单)
8. [回滚方案](#8-回滚方案)
9. [分阶段迁移路线](#9-分阶段迁移路线)
10. [常见迁移问题 FAQ](#10-常见迁移问题-faq)
11. [附：时间估算](#11-附时间估算)

---

## 1. 迁移策略总览

v1 与 v2 在进程外数据（sessions / ctx / traces / cron / workspace / pgvector）上**完全向前兼容**，真正的变更集中在：①凭证管理方式 ②config.yaml 新节点 ③SKILL.md frontmatter 可选字段 ④pgvector 的可选 `NOT NULL` 约束。

因此有三种升级路径可选，按风险和停机时间排序：

| 策略 | 停机时间 | 数据迁移复杂度 | 适用场景 | 回滚难度 |
|---|---|---|---|---|
| **A. 蓝绿部署**（推荐） | ≤ 30s（飞书 WebSocket 重连窗口） | 低（只读挂载 `data/`，切换后转主） | 生产环境、有独立 canary | 低（切 DNS / 端口即可） |
| **B. 停机升级** | 5-15 min（数据复制 + 启动） | 中（停服复制 `data/` 目录） | 小规模内部使用、无 canary | 中（重启 v1 即可） |
| **C. 原地升级** | ≤ 2 min（重启服务） | 高（v1 stop → 改 config → v2 start） | 开发/测试环境 | 高（配置已改，回滚需撤销） |

**推荐策略 A**。本指南默认按 A 组织步骤；B / C 在 §9 给出差异化说明。

**关键前提**：
- v2 不支持多节点（见 DESIGN §2.4），升级不改变拓扑
- v1 **凭证视为已泄露**，Phase 0 必须先轮换
- `data/` 目录格式向前兼容，v2 读 v1 数据无需转换

---

## 2. Phase 0 预准备

**时长：1.5 工作日**。Phase 0 是不可跳过的前置工作，失败影响后续所有 Phase。

### 2.1 凭证轮换清单

所有"已入库的凭证"视为已泄露（v1 `config.yaml` checked-in 过）。必须在 Phase 1 启动前全部轮换：

```
□ PostgreSQL xiaopaw_memory 数据库用户
  - 新建 user：CREATE USER xiaopaw_v2 WITH PASSWORD '<32 字符强密码>';
  - 授权：GRANT ALL ON SCHEMA public TO xiaopaw_v2;
  - 旧 user 暂留（不删，灰度期回滚用），DROP 留到 Phase 5

□ 飞书 App Secret
  - 开放平台后台 → 凭证与基础信息 → 重新生成 App Secret
  - 更新所有已部署实例（v1 + canary）

□ Qwen API Key（DashScope）
  - 控制台 → API-KEY 管理 → 新建 key
  - 旧 key 灰度期保留（v1 仍在跑）

□ 百度千帆 API Key（如启用）
  - 同上

□ XIAOPAW_METRICS_TOKEN（如启用 /metrics）
  - 生成 ≥ 32 字符随机 token
  - 用 openssl rand -hex 32
```

### 2.2 Canary 环境就绪

```bash
# 独立机器或 VM
# 1. 部署 pgvector 独立副本（与 prod 不共库）
docker compose -f pgvector-docker-compose.yaml up -d

# 2. 部署独立 AIO-Sandbox
docker compose -f sandbox-docker-compose.yaml up -d

# 3. 挂 Prometheus + pytest-memray 采集
#    目的：跑 72h baseline 观察内存曲线
```

**Canary 准入**：连续 24h smoke test `runner_alive=1` 且内存增长斜率 < 1MB/h。

### 2.3 Tokenizer 校准

v2 `memory/token_counter.py` 优先使用 Qwen 官方 tokenizer，但不同版本 dashscope SDK 支持情况不一，必须先校准。

```bash
# 产出 docs/tokenizer-calibration.md
python scripts/calibrate_tokenizer.py \
  --samples data/samples/real_messages_100.txt \
  --models qwen-max \
  --fallbacks tiktoken,rough \
  --output docs/tokenizer-calibration.md
```

校准报告决定：
- 首选：`dashscope.get_tokenizer("qwen-max")` 可用 → FeatureFlag `token_counter_mode=qwen_official`
- 次选：HuggingFace `Qwen/Qwen2-7B` tokenizer → 本地加载
- 兜底：`len // 2` → FeatureFlag `token_counter_mode=rough`（开发环境默认）

### 2.4 备份与 force-push 通告

```bash
# 对 v1 代码仓打标签（凭证剥离前留根）
cd xiaopaw-with-memory
git tag v-pre-hardening
git push origin v-pre-hardening

# 对 v1 数据目录打快照
tar czf backup/xiaopaw-v1-data-$(date +%Y%m%d).tar.gz data/
# pgvector 逻辑备份
pg_dump -h pgvector -U xiaopaw xiaopaw_memory \
  > backup/xiaopaw-v1-pgvector-$(date +%Y%m%d).sql
```

### 2.5 Phase 0 交付物

- [ ] `docs/phase0-checklist.md` 全部勾完
- [ ] `docs/tokenizer-calibration.md` 报告生成
- [ ] Canary 24h smoke 监控截图
- [ ] 凭证轮换完成清单（执行人 / 时间 / 旧凭证失效证据）
- [ ] v1 数据目录 + pgvector dump 已归档到独立存储

---

## 3. 数据兼容性矩阵

v1 → v2 所有持久化数据的兼容性结论。详细字段展开见 [03-data.md §15](03-data.md)。

| 数据 | 路径 | v1 → v2 | 迁移动作 |
|---|---|---|---|
| Session 索引 | `data/sessions/index.json` | ✓ 完全兼容 | 直接复制 |
| 对话历史 | `data/sessions/{sid}.jsonl` | ✓ 完全兼容 | 直接复制；v2 读取时 `asyncio.to_thread` 倒序流式读 |
| ctx snapshot | `data/ctx/{sid}_ctx.json` | ✓ 完全兼容 | 直接复制 |
| raw audit | `data/ctx/{sid}_raw.jsonl` | ✓ 完全兼容 | 直接复制；30 天以外的可预清理减少体量 |
| Traces | `data/traces/{sid}/{ts}_{msg_id}/` | ✓ 完全兼容 | 直接复制；30 天以外的可预清理 |
| Cron tasks | `data/cron/tasks.json` | ✓ 完全兼容 | 直接复制；v2 首次启动补齐 `tasks.json.lock` 文件 |
| Cron DLQ | `data/cron/tasks.dlq.jsonl` | v1 无 | 无需迁移（v2 空文件起步） |
| pgvector `memories` 表 | PG 数据库 | ✓ 完全兼容 | `CREATE TABLE IF NOT EXISTS` 幂等；`ALTER TABLE ... SET NOT NULL` **大表需两阶段 SOP**（见 §4.2，`ADD CONSTRAINT NOT VALID` + `VALIDATE CONSTRAINT` 避免锁表） |
| Workspace 会话文件 | `data/workspace/sessions/{sid}/` | ✓ 完全兼容 | 直接复制 |
| Workspace credentials | `data/workspace/.config/{feishu,baidu}.json` | ✓ 格式兼容 | **关键路径（v2.1 修正，P0-8）**：v2 sandbox compose 保留**整个 `./data/workspace` mount**（含 `.config/` 子目录），**不是**仅挂 session 子目录。CleanupService 启动约 t+1s 从 docker secrets 读 `feishu_app_id`/`feishu_app_secret`/`baidu_api_key` 并重写 `.config/feishu.json` / `.config/baidu.json`；首次 Skill 调用前凭证就位，避免挂载断链 |
| Workspace memory | `data/workspace/{soul,user,agent,memory}.md` | ✓ 完全兼容 | 直接复制 |
| SKILL.md frontmatter | `xiaopaw/skills/*/SKILL.md` | ⚠️ **不兼容（v2.1 修正）** | v2 会**覆盖** `memory-save` / `search_memory` / `skill-creator` / `memory-governance` 四个核心 Skill 的 SKILL.md（行为含 `BLOCKED_PATTERNS` / routing_key 校验等安全加固）。**v1 用户自定义**修改过这些 SKILL.md，升级后须重新补齐 `allowed_tools` 字段并合并 v2 上游变更。用户自建 Skill（非四个核心）不受覆盖影响 |
| `config.yaml` | 仓库内 | ✗ 需改造 | 新增 `feature_flags:` 节；凭证全部移到 `.env`（见 §5） |
| 代码 | `xiaopaw/` | ✗ 大幅重构 | 非"升级"，是"切版本"：部署 v2 二进制，不原地改 v1（见 §6） |

**核心结论**：持久化数据侧是**复制即可**。真正要改的是代码部署物 + config.yaml + `.env`。

---

## 4. Schema 变更

### 4.1 幂等创建（全新部署和升级通用）

v2 的 `schema.sql` 在 v1 基础上加了一行：`routing_key TEXT NOT NULL`。用 `CREATE TABLE IF NOT EXISTS` 时，**若表已存在，v1 的 schema 不会被改变**（PostgreSQL 不会重新应用约束）。因此升级时需要显式 `ALTER`：

```bash
# Step 1: 幂等创建（对新 DB 生效；已有表无副作用）
psql "$MEMORY_DB_DSN" -f schema.sql
```

### 4.2 补强 `routing_key NOT NULL` 约束（必做，含大表两阶段）

v1 的 `routing_key` 字段实际都有值（`async_index_turn` 链路强制填充），但 DDL 层面未强制。v2 加约束以纵深防御 H1（跨用户隔离）。

**Step 0：先确认 v1 无 NULL 行**

```sql
SELECT COUNT(*) FROM memories WHERE routing_key IS NULL;
-- 期望：0
```

**如果 Step 0 返回非 0**：先跑清理脚本（通常是历史测试数据）：

```sql
-- 谨慎：先备份受影响行
COPY (SELECT * FROM memories WHERE routing_key IS NULL)
  TO '/tmp/null_routing_key_backup.csv' CSV HEADER;

-- 删除 NULL 行（测试数据场景）或修复 routing_key
DELETE FROM memories WHERE routing_key IS NULL;
```

#### 小表方案（`< 10 万行`）：直接 ALTER

```sql
-- 小表 AccessExclusiveLock 持有 < 1s，可接受
ALTER TABLE memories ALTER COLUMN routing_key SET NOT NULL;

-- 验证
\d memories
-- routing_key | text | not null
```

#### 大表方案（`> 100 万行`）：两阶段，避免锁表

**v2.1 新增**：`ALTER TABLE ... SET NOT NULL` 直接执行会**扫全表**并持 `AccessExclusiveLock`，百万级表可能锁 30s 以上，生产不可接受。两阶段方案把"校验"与"约束化"解耦：

```sql
-- 阶段 1：加 CHECK 约束 NOT VALID（不扫全表，毫秒级 DDL）
--   NOT VALID 告诉 PG：新插入/更新的行要校验，但不对存量行扫描。
--   锁级别：ShareUpdateExclusiveLock（允许 SELECT/INSERT/UPDATE/DELETE 并发）。
ALTER TABLE memories
  ADD CONSTRAINT memories_routing_key_not_null
  CHECK (routing_key IS NOT NULL) NOT VALID;

-- 阶段 2：后台验证（只加 ShareUpdateExclusiveLock，允许 DML）
--   VALIDATE CONSTRAINT 会顺序扫全表确认约束成立，但不阻塞业务读写。
--   百万行约 30-60s，可以跟业务并发。
ALTER TABLE memories VALIDATE CONSTRAINT memories_routing_key_not_null;

-- 阶段 3（可选）：转为标准 NOT NULL
--   PG 12+ 识别到已验证的 CHECK (col IS NOT NULL) 时，SET NOT NULL 会跳过全表扫描，
--   仅做目录元数据变更（毫秒级 AccessExclusiveLock）。PG 11- 仍会扫全表，可跳过此步，
--   CHECK 约束在语义上已等价于 NOT NULL。
ALTER TABLE memories ALTER COLUMN routing_key SET NOT NULL;

-- 回滚（见 §8.4）
-- ALTER TABLE memories DROP CONSTRAINT memories_routing_key_not_null;
-- ALTER TABLE memories ALTER COLUMN routing_key DROP NOT NULL;
```

**判据**：在 canary 上先 `SELECT count(*) FROM memories;`，≥ 100 万走大表方案，否则小表。

### 4.3 RLS（可选，多租户部署时强制）

单节点单租户场景可**不启用**；多租户或有合规要求时必启。详细见 [03-data.md §10.5](03-data.md)。

```sql
-- 启用 RLS
ALTER TABLE memories ENABLE ROW LEVEL SECURITY;

-- 建策略：查询时必须设 current_setting('xiaopaw.current_routing_key')
CREATE POLICY memories_isolation ON memories
    USING (routing_key = current_setting('xiaopaw.current_routing_key', true));

-- 应用侧必须在每次查询前
-- SET LOCAL xiaopaw.current_routing_key = 'p2p:ou_abc123';
```

**开关**：`config.yaml` → `feature_flags.enable_pgvector_rls=true`（v2 默认关）。

### 4.4 索引复查

v1 已建 HNSW / GIN / btree 索引，v2 不变。可选做 REINDEX 减少升级后的查询抖动：

```sql
REINDEX TABLE CONCURRENTLY memories;  -- CONCURRENTLY 不阻塞读写
```

---

## 5. 配置变更

### 5.1 凭证全部移到 `.env`

v1 `config.yaml` 混合写入凭证（即使是 `${FEISHU_APP_SECRET}` 占位符）。v2 强制：**`config.yaml` 不含任何凭证字段值**（可保留 key 名作模板）。

**v2 `.env` 最小字段清单**（见 `.env.example`）：

```bash
# 飞书
FEISHU_APP_ID=cli_xxxxxxxx
FEISHU_APP_SECRET=<32+ 字符，已轮换>
FEISHU_ENCRYPT_KEY=<可选>
FEISHU_VERIFICATION_TOKEN=<可选>

# Qwen
QWEN_API_KEY=sk-xxxxxxxx

# pgvector
MEMORY_DB_DSN=postgresql://xiaopaw_v2:<强密码>@pgvector:5432/xiaopaw_memory

# 百度（可选）
BAIDU_API_KEY=<key>

# 观测
XIAOPAW_METRICS_TOKEN=<32 字符 hex>
XIAOPAW_TESTAPI_TOKEN=<32 字符 hex，prod 不需要>

# 环境标记
XIAOPAW_ENV=prod   # dev / canary / prod
```

### 5.2 `config.yaml` 增量

v2 `config.yaml` 相对于 v1 的 diff（省略未变字段）：

```yaml
# --- v1 保留字段（不变）---
workspace:
  id: "xiaopaw-default"
  name: "XiaoPaw 工作助手"

bot:
  loading_message: "思考中..."
  prefix: ""

agent:
  model: "qwen3-max"
  max_iter: 50
  max_input_tokens: 30000
  # ... 其他不变

# --- v2 新增字段 ---

feishu:
  # app_id / app_secret 从 config.yaml 删除；v2.1 统一走 docker secrets，
  # 由 entrypoint.sh 从 /run/secrets/feishu_* 读。占位符也不留。
  allowed_chats: []                     # v2 白名单
  inbound_rate_limit:                   # v2 新增
    per_user_per_minute: 20

sender:
  max_retries: 3
  retry_backoff: [1, 2, 4]
  max_concurrent: 5                      # v2 新增：Semaphore 上限

session:
  max_history_turns: 20
  max_active_sessions: 1000              # v2 新增：LRUCache 上限

memory:
  workspace_dir: "./data/workspace"
  ctx_dir: "./data/ctx"
  max_memory_file_lines: 250             # v2.1 新增：memory.md 行数上限（超则 governance 触发）
  # db_dsn 从 config.yaml 删除，只从 docker secrets 读

sandbox:
  # v2.1 修正：容器间 URL 用容器内部端口 8080，不是 v1 的宿主机映射 8022
  url: "http://aio-sandbox:8080/mcp"

cron:
  file_lock_timeout_s: 10                # v2 新增

debug:
  enable_test_api: false                 # prod 强制 false
  test_api_port: 9090
  test_api_host: "127.0.0.1"             # v2 强制 loopback

# v2 新增节 ↓↓↓
observability:
  # v2.1：统一端口 8090（/metrics 与 /health 同端口，aiohttp 子路由分发）
  metrics_port: 8090
  # health_port 字段 v2.1 删除（与 metrics_port 同一端口，不再分两个）
  enable_trace_id: true

feature_flags:
  # v2.1：三值 Literal，覆盖 HuggingFace fallback
  token_counter_mode: "qwen_official"    # qwen_official / hf_qwen / rough
  enable_skill_timeout: true
  enable_cron_filelock: true
  enable_memory_save_filelock: true
  enable_feishu_rate_limit_aware: true
  enable_mcp_whitelist: true             # v2.1 默认 true；开发环境可关
  enable_memory_save_filter: true
  # v2.1 改名：原 enable_webhook_signature → enable_webhook_replay_cache
  # 原因：WS 模式 lark-oapi 无 encrypt_key 验签 API；应用层用 event_id LRU+TTL 做重放防护
  enable_webhook_replay_cache: true
  enable_inbound_rate_limit: true
  enable_pgvector_rls: false             # 多租户时开
  # v2.1 新增：psycopg2.pool.ThreadedConnectionPool 开关
  enable_pgvector_connection_pool: true
```

### 5.3 `.gitignore` 补齐

```
# .gitignore（v2）
config.yaml
.env
data/
.pytest_cache/
.coverage
coverage.json
```

### 5.4 启动前自检

v2 首次启动会跑 `assert_production_safe(cfg)`（详见 [07-security.md](07-security.md)）：

- `XIAOPAW_ENV=prod` 时 `debug.enable_test_api` 必须 `false`
- DSN 密码走 `is_weak_password` 校验（正则 + hash）
- `feishu.app_secret` 非空且非弱密码
- `/metrics` Bearer Token ≥ 32 字符

任何失败直接拒绝启动。

---

## 6. 代码变更对照表

v1 → v2 的代码不是"原地修改"而是**整个替换**。部署上只需切二进制/切镜像，但实施前要知道每个模块的变化点，便于定位问题。

| 模块 | v1 文件 | v2 文件 | 主要改动 | Feature Flag 可关 |
|---|---|---|---|---|
| 入口 | `xiaopaw/main.py` | `xiaopaw/main.py` | 增加 `assert_production_safe()` 启动自检 | — |
| 配置 | `xiaopaw/main.py`（内联） | `xiaopaw/config/validator.py` / `safety.py` / `flags.py` | 抽独立模块；Pydantic 校验；正则+hash 密码检测；FeatureFlags registry | — |
| 运行时调度 | `xiaopaw/runner.py` | `xiaopaw/runner.py` | 加 `_queue_gen` 计数器；`_pending_index_tasks: set`；`_dispatch_lock`；worker `finally` 按 gen 清理 | — |
| Session 管理 | `xiaopaw/session/manager.py` | `xiaopaw/session/manager.py` | `_jsonl_locks` 改 `LRUCache(1000)` + `_dispatch_lock` 保 check-and-create 原子（v2.1 避免驱逐双锁，见 concurrency-verification-report §1）；`load_history` 用 `asyncio.to_thread` 倒序流式读 | — |
| 飞书监听 | `xiaopaw/feishu/listener.py` | `xiaopaw/feishu/listener.py` | **v2.1 修正**：WS 模式 SDK 无 `encrypt_key`/`verification_token` 参数（见 sdk-verification-report §1），**该参数不存在，应用层实现 ReplayCache**（`event_id -> ts` LRU + TTL 5min）；`RateLimiter`（每用户 20/分钟） | `enable_webhook_replay_cache` (F9) / `enable_inbound_rate_limit` (F10) |
| 飞书发送 | `xiaopaw/feishu/sender.py` | `xiaopaw/feishu/sender.py` | **v2.1 修正**：错误码不绑死 `99991663/72` 白名单（未能证实，见 sdk-verification-report §4），改 `resp.code != 0 + msg 关键词匹配`；`resp.raw_response` → `resp.raw`（SDK 真实属性名）；HTTP 层 429 读 `resp.raw.headers.get("Retry-After")`；`Semaphore(5)` 控并发 | `enable_feishu_rate_limit_aware` (F5) |
| 主 Crew | `xiaopaw/agents/main_crew.py` | `xiaopaw/agents/main_crew.py` | `run_and_index` 不再 `create_task`，只暴露 `_index_coroutine` 给 Runner | — |
| Skill Loader | `xiaopaw/tools/skill_loader.py` | `xiaopaw/tools/skill_loader.py` | `asyncio.wait_for` 超时（默认 120s）+ sandbox 主动 kill；`search_memory` routing_key 校验拒绝而非覆盖；MCP 白名单；`_run` 用 `ThreadPoolExecutor` 包 `asyncio.run` 而非 `nest_asyncio` | `enable_skill_timeout` (F2) / `enable_mcp_whitelist` (F7) |
| Token 计数 | 内联 `len//2` | `xiaopaw/memory/token_counter.py` | 三级降级：Qwen 官方 tokenizer → HuggingFace `Qwen/Qwen2-7B` → `len // 2`（v2.1：三值 Literal） | `token_counter_mode` (F1) |
| Indexer | `xiaopaw/memory/indexer.py` | `xiaopaw/memory/indexer.py` | **v2.1 修正**：连接池改 `psycopg2.pool.ThreadedConnectionPool`（v1 psycopg2 生态；`psycopg_pool` 属 psycopg3 生态，与 psycopg2 不兼容，见 sdk-verification-report §5）；DB 调用 `asyncio.to_thread` 包；`@cache` 单例 LLM / embedding 客户端 | `enable_pgvector_connection_pool` (F12) |
| Cron | `xiaopaw/cron/service.py` | `xiaopaw/cron/service.py` + `storage.py` | 新增 `storage.py`：`filelock.FileLock` + DLQ；失败不推 `next_run_at`；Pydantic schema 校验 payload（T8/T10 防 cron 注入） | `enable_cron_filelock` (F3) |
| Cleanup | `xiaopaw/cleanup/service.py` | `xiaopaw/cleanup/service.py` | 基本不变；**v2.1**：`write_feishu/baidu_credentials` 从 docker secrets 读（`/run/secrets/feishu_app_id` 等），不从 `.env` 读 | — |
| 日志 | `xiaopaw/observability/logging_config.py` | `xiaopaw/observability/logging_config.py` | JSON log + caller + stacktrace | `enable_trace_id` (F6) |
| Trace | — | `xiaopaw/observability/trace.py` | v2 新增：`ContextVar` + `run_in_executor_with_context` helper（**v2.1 更正**：`run_in_executor` 所有版本都不 copy_context，非 Py≤3.13 专属问题；`to_thread` 3.9+ 即自动） | `enable_trace_id` (F6) |
| PII | — | `xiaopaw/observability/pii_mask.py` | v2 新增：手机/邮箱/身份证/银行卡落盘前 mask | — |
| Metrics | `xiaopaw/observability/metrics.py` | `xiaopaw/observability/metrics.py` | 精简为 8 指标 | — |
| Metrics server | `xiaopaw/observability/metrics_server.py` | `xiaopaw/observability/metrics_server.py` | Bearer Token + `constant_time_equals`；prod 强制 token；**v2.1**：与 `/health` 同 aiohttp Application，端口统一 **8090** | — |
| 重试 | 散落 | `xiaopaw/utils/retry.py` | v2 新增：`external_api_retry(exc_types: tuple)` 工厂 | — |
| memory-save Skill | `xiaopaw/skills/memory-save/scripts/save.py` | 同路径 | `BLOCKED_PATTERNS` 过滤 + `filelock` 每 topic 锁 + 长度上限 2000 | `enable_memory_save_filter` (F8) / `enable_memory_save_filelock` (F4) |
| search_memory Skill | `xiaopaw/skills/search_memory/scripts/search.py` | 同路径 | `routing_key` required（脚本级） | `enable_pgvector_rls` (F11) DB 层兜底 |
| 其他 Skills | `xiaopaw/skills/*/SKILL.md` | 同路径 | frontmatter 可选增加 `allowed_tools` | `enable_mcp_whitelist` (F7) |
| TestAPI | `xiaopaw/api/test_server.py` | `xiaopaw/api/test_server.py` | Bearer Token + loopback bind；prod 拒绝启动 | — |

**迁移使用要点**：
- v2 是**独立代码目录**（`xiaopaw-v2/` 与 `xiaopaw-with-memory/` 平行），不要原地改 v1
- 共享 `data/` 目录时必须保证同一时刻**只有一个**进程写（蓝绿切换时用只读挂载防双写）
- Docker 镜像用 tag + digest 锁版本：`image: ghcr.io/xiaopaw/xiaopaw-v2:v2.0.0@sha256:<digest>`

---

## 7. 测试回归清单

切流量前必须在 canary 通过的验证项。

### 7.1 基础冒烟（30 分钟）

```bash
# 1. 启动自检通过
XIAOPAW_ENV=prod python -m xiaopaw.main --check-only
# 期望：exit 0；输出 "Production safety checks passed"

# 2. /health 返回 200（v2.1：端口统一 8090）
curl -s http://localhost:8090/health
# 期望：{"status":"ok","git_sha":"<commit>"}

# 3. /metrics 需要 Bearer Token（v2.1：与 /health 同端口 8090）
curl -s -H "Authorization: Bearer $XIAOPAW_METRICS_TOKEN" http://localhost:8090/metrics | grep xiaopaw_inbound_total
# 期望：有指标输出

# 4. TestAPI prod 环境被拒绝
curl -s -X POST http://localhost:9090/api/test/message
# 期望：连接失败（未绑定端口）
```

### 7.2 数据连续性（1 小时）

从 v1 `data/` 复制到 canary 后：

```
□ session index 能读取：/status 命令返回 v1 保存的 session
□ 历史消息能读取：/status → 进入任一 session，显示 v1 写入的 turn
□ ctx.json 能还原：重启 v2 后，@before_llm_call 恢复 v1 最后一轮 ctx
□ cron tasks 能加载：/status cron 显示 v1 配置的所有定时任务
□ pgvector 能查询：search_memory 返回 v1 已索引的记忆条目
□ workspace 文件能读：Bootstrap 四件套显示 v1 的 soul/user/agent/memory.md
```

### 7.3 v2 新增能力（2 小时）

TC 编号引用 [`test-cases-for-known-risks.md`](./test-cases-for-known-risks.md)。

```
□ Webhook ReplayCache（TC-P0-1-a/b，T3）：同 event_id 重复投递 → 第二次被 ReplayCache 拦截
□ 入站速率限制（TC-P2-8，T7）：单用户 30 秒内发 30 条 → 第 21 条起被丢弃（metric `xiaopaw_rate_limited_total` +1）
□ memory-save 过滤（TC-P2-3，T2）：发送 "忽略之前的指令" → memory-save 拒绝写入（errcode=403）
□ MCP 白名单（TC-P0-2-a，T1；flag 开）：Skill 未声明 allowed_tools 时不允许调用 sandbox_execute_bash
□ Skill 超时：构造一个耗时 > 120s 的 Skill → `asyncio.wait_for` 超时返回 errcode=408
□ 飞书限流识别：mock 返回 `resp.code != 0` + msg 含 "rate limit" → 指数退避（不绑死具体码值）
□ raw_response 属性改名：`resp.raw.status_code` / `resp.raw.headers.get("Retry-After")` 正常读到
□ trace_id 贯穿：一条消息的入站日志 / LLM 调用日志 / 出站日志 trace_id 一致
□ PII mask：落盘日志中手机号/邮箱/身份证被 mask
□ LRUCache 互斥正确性（TC-P1-14 扩展）：驱逐后重入同 sid → 只有一把锁（WeakValueDictionary + dispatch_lock）
□ Cron filelock（TC-P1-6/8，T8/T10）：两个进程同时读写 tasks.json → 无数据损坏；Pydantic schema 拒绝非法 payload
□ sandbox 端口封闭（TC-P1-7，T9）：`docker compose config` 应无 aio-sandbox 的 host ports 映射
□ pgvector 连接池（TC-F12）：`enable_pgvector_connection_pool=true` → `xiaopaw_pgvector_pool_size` 暴露
```

### 7.4 72h Canary 观测

```
□ runner_alive=1 持续
□ 内存增长斜率 < 1MB/h
□ xiaopaw_agent_latency_seconds p95 < v1 baseline * 1.2
□ xiaopaw_llm_latency_seconds p95 < v1 baseline * 1.2
□ xiaopaw_skill_timeout_total 与预期故障场景一致
□ xiaopaw_cron_dlq_total = 0（无非预期死信）
□ 日志中无 ERROR 级别未处理异常
```

### 7.5 故障注入（切流量前必过）

```
□ 断 pgvector：search_memory 降级但不崩；恢复后自动继续索引
□ 断 Qwen API：tenacity 重试后 503 返回；canary 存活
□ 断 AIO-Sandbox：SkillLoader 超时；主 Agent 返回错误给用户
□ 磁盘写满 data/：ENOSPC 被捕获；runner_alive=1
□ 飞书 429：sender 指数退避；不影响队列消费
```

---

## 9. 分阶段迁移路线

### 9.1 Phase 映射（v2.1：重命名避免同名冲突）

v2 的重构按 6 个**开发 Phase** 交付代码（见 [重构设计 v2](../../../multi-agent/review_L22/02_重构设计文档_xiaopaw-with-memory_v2.md) §2）；迁移本身走独立的 **Rollout Phase** 编号。为避免两套 Phase 互相混淆，v2.1 明确重命名：

| v2.1 开发 Phase（代码交付） | 对应 Rollout Phase（部署步骤） | 时长 |
|---|---|---|
| Dev-P0（凭证/canary/tokenizer 前置） | Rollout-P0：§2 预准备 | 3 自然日（含 force-push 48h 通告 + 凭证审批） |
| Dev-P1（Blocker + 横切 trace_id + FeatureFlags） | Rollout-P1：§5 配置变更 + 部署 v2 代码 | 0.5 天 |
| Dev-P2（并发 & 资源治理） | Rollout-P2：§9.2 canary 启动 + baseline | 0.5 天 |
| Dev-P3（容错 & 降级） | Rollout-P3：§9.3 canary 72h 观测 | 72h |
| Dev-P4（观测 & 安全规范） | Rollout-P4：§9.4 蓝绿切换 | ≤ 5 min（飞书 WS 重连 5-30s + 冒烟） |
| Dev-P5-6（威胁建模 / 合规 / 测试 / CI） | Rollout-P5：§9.5 v1 下线 | 1 天 |

### 9.2 Phase 1：v2 canary 启动（0 流量）

```bash
# Step 1: 拉 v2 镜像
docker pull ghcr.io/xiaopaw/xiaopaw-v2:v2.0.0

# Step 2: 凭证 + config 就位
cp xiaopaw-v2/.env.example .env
# 编辑 .env 填入 Phase 0 轮换后的凭证

cp xiaopaw-v2/config.yaml.example config.yaml
# 修改 config.yaml，按 §5.2 对比

# Step 3: 运行 schema 幂等迁移
psql "$MEMORY_DB_DSN" -f xiaopaw-v2/schema.sql

# Step 4: 启动 canary（不切飞书 WebSocket，只自己用 TestAPI 打）
docker compose -f xiaopaw-docker-compose.yaml up -d

# Step 5: 验证启动自检
docker logs xiaopaw-v2 | grep "Production safety checks passed"
```

### 9.3 Phase 2-3：72h Canary 观测

canary 需要接真实数据。做法：

**方案 A（推荐）**：复制 v1 `data/` 只读挂载到 canary，TestAPI 回放历史消息：

```bash
# 只读复制（避免双写污染 v1）
rsync -a --delete \
  xiaopaw-with-memory/data/ \
  xiaopaw-v2-canary/data/

# 在 canary TestAPI 回放
python scripts/replay_sessions.py \
  --source xiaopaw-v2-canary/data/sessions \
  --endpoint http://canary:9090/api/test/message \
  --token "$XIAOPAW_TESTAPI_TOKEN"
```

**方案 B**：镜像飞书流量（需飞书侧支持多 App），canary 订阅另一个测试 App。

**观测关键指标**：内存曲线、延迟 p95、指标齐全度、trace_id 覆盖率。详见 §7.4。

### 9.4 Rollout-P4：蓝绿切换（用户可感知停机 ≤ 5 min）

```bash
# T+0: 停止 v1 向 data/ 写入
docker stop xiaopaw-v1  # 飞书 WebSocket 断开

# T+1: v2 data/ 改为读写挂载（之前是只读）
#      最简单：v2 用同一个 data/ 目录，v1 停止后无冲突
docker compose -f xiaopaw-docker-compose-prod.yaml up -d xiaopaw-v2

# T+2: 飞书事件回推到 v2（App 已在 v2 重启后建立 WebSocket）
#      lark-oapi 客户端重连窗口约 10-30s

# T+3: 验证
curl -s http://v2:8080/health
# 期望：{"status":"ok"}
tail -f data/logs/xiaopaw.log | grep trace_id
# 期望：有新 trace_id 进入

# T+60s: 发一条测试消息到真实飞书群
# 期望：v2 正常回复，有 trace_id 贯穿
```

**切换窗口关键点**：
- lark-oapi 客户端重连实测 **5-30s**（v2.1 修正，不是原文的 10-30s 下限）——取决于心跳超时 + app_secret 握手 + 事件订阅恢复
- 切换期间用户发的消息由飞书侧队列暂存（通常 ≥ 5 分钟），v2 上线后推送
- 用户可感知停机含冒烟验证 → RTO 目标 **≤ 5 min**（v2.1 从原 2 min 放宽）
- 如切换 > 5 min 仍未恢复，立刻回滚（见 §8）

### 9.5 Phase 5：v1 下线

canary 跑满 72h 无红线指标后：

```bash
# Step 1: 确认 v2 7 天无回滚事件
# Step 2: 备份 v1 最终状态
tar czf backup/xiaopaw-v1-final-$(date +%Y%m%d).tar.gz xiaopaw-with-memory/data/

# Step 3: pgvector 旧 user 清理
psql -h pgvector -U postgres -d xiaopaw_memory << EOF
REVOKE ALL ON SCHEMA public FROM xiaopaw;
DROP USER xiaopaw;
EOF

# Step 4: Qwen / 飞书旧 key 作废
# 飞书开放平台：删除旧 App Secret
# DashScope：删除旧 API Key

# Step 5: 归档 v1 代码目录（不删除，留作追溯）
mv xiaopaw-with-memory xiaopaw-with-memory.archived
```

---

## 8. 回滚方案

每个 Phase 都必须有明确回滚路径。**回滚窗口：切换后 72h 内**；超过则需评估 v1 代码与 data/ 的漂移。

### 8.1 Phase 0 / Phase 1 回滚（凭证过渡窗口）

- 凭证轮换是**单向**操作：新凭证已发到 canary，无法回滚到"未轮换"状态。
- **v2.1 凭证过渡窗口：24h**。飞书开放平台生成新 App Secret 后，旧 Secret 有 24h 宽限期（平台实际常超过，但按 24h 规划）。Phase 1 期间允许新旧凭证**短暂并存**：
  - canary 用新凭证；prod v1 还用旧凭证 → 两套独立服务，互不干扰
  - 若 Phase 4 切换失败回 v1，旧凭证仍可用于 v1（前提是 24h 未超期）
  - 超过 24h 后若仍需回退到 v1，必须在飞书后台再轮换一次（新生成 Secret 给 v1 用）
- Tokenizer 校准是纯文档输出，无副作用

### 8.2 Phase 1-2 回滚（canary 启动失败）

canary 独立环境，与 prod 无交叉。直接：

```bash
docker compose -f xiaopaw-docker-compose.yaml down
rm -rf xiaopaw-v2-canary/data/
# 恢复配置即可
```

prod v1 完全不受影响。

### 8.3 Phase 3 回滚（canary 观察期发现问题）

不切流量，v1 继续服务。根据问题严重度：

| 问题 | 处理 |
|---|---|
| FeatureFlag 可关的行为 | 改 `config.yaml.feature_flags` → SIGHUP 重载 → 继续观察 |
| 代码 bug（影响 canary） | 修复 → 重新部署 canary → 延长观察 |
| 架构级问题 | 退回 Phase 2，重新评估方案 |

### 8.4 Phase 4 回滚（蓝绿切换失败）

**关键**：data/ 已被 v2 写了若干分钟。v1 回滚要处理数据兼容性。

```bash
# Step 1: 停 v2
docker stop xiaopaw-v2

# Step 2: 检查 v2 写入期间的增量
ls -lt data/sessions/*.jsonl | head -5
# 若 v2 新增了会话数据：v1 可直接读（格式兼容）

# Step 3: 特殊情况处理
# 3a. pgvector 有新 routing_key NOT NULL 约束：v1 照样写得进去（v1 自己就填值）
# 3b. DLQ 文件（v1 不识别）：v1 忽略，无副作用
# 3c. config.yaml 被改过：还原 v1 config.yaml（备份文件）
# 3d. pgvector schema 回滚（v2.1 大表两阶段的反向操作）：
#     DROP CONSTRAINT + DROP NOT NULL 两步，都是毫秒级 DDL
#     psql -c "ALTER TABLE memories DROP CONSTRAINT IF EXISTS memories_routing_key_not_null;"
#     psql -c "ALTER TABLE memories ALTER COLUMN routing_key DROP NOT NULL;"

# Step 4: 启动 v1
docker start xiaopaw-v1
tail -f data/logs/xiaopaw.log
```

**回滚边界**：若 v2 在切换窗口内已写入 pgvector 且该表启用了 RLS 或其他 v2-only 约束，v1 可能无法写入（v1 不 `SET LOCAL xiaopaw.current_routing_key`）。此时：
- 临时关 RLS：`ALTER TABLE memories DISABLE ROW LEVEL SECURITY;`
- 完成 v1 回滚后再决定是否重开

### 8.5 Phase 5 回滚（v1 已下线）

v1 下线 = 旧凭证作废 + v1 目录归档。**不建议**在此阶段回滚，应走新一轮 canary。

如必须回滚：

```bash
# Step 1: 恢复旧凭证（飞书 App / Qwen key）
# Step 2: 从归档恢复 v1 代码
mv xiaopaw-with-memory.archived xiaopaw-with-memory
# Step 3: 恢复凭证到 v1 config.yaml（v1 不支持 .env）
# Step 4: 启动 v1，同时停 v2
```

---

## 10. 常见迁移问题 FAQ

### Q1: 为什么 v1 data/ 能直接被 v2 读？

A: v2 的数据设计是 v1 的**严格超集**。JSONL / JSON / pgvector schema / workspace 文件格式都保持兼容。唯一例外是 `config.yaml`（结构变更）和 `.env`（v1 没有）。

### Q2: pgvector 的 `routing_key NOT NULL` 约束，v1 运行期间会被触发吗？

A: 不会。v1 代码路径在 `upsert_memory` 时必然填充 `routing_key`（从 `SessionManager` 拿 routing_key 传入），约束只是**兜底校验**而非**改变行为**。所以升级后 v1 和 v2 可以继续共用同一张表。

### Q3: 切换窗口用户发的消息会丢吗？

A: 飞书 WebSocket 在客户端断连后，服务端会缓存事件一段时间（通常 ≥ 5 分钟）。v2 重连后事件会重推。实际观察到的"丢失"大多是 v2 启动失败导致超出缓冲窗口。因此 Phase 0 canary 24h smoke 至关重要。

### Q4: 开发环境要不要改成 v2？

A: 不建议。v2 多了凭证自检 / 验签 / MCP 白名单等机制，开发环境会徒增门槛。保留 v1 作参考示例，v2 作"生产加固参考"。两者代码目录并行存在。

### Q5: SKILL.md 是否必须补齐 `allowed_tools`？

A: 取决于 `feature_flags.enable_mcp_whitelist`：
- `false`（默认，开发兼容）：不必补；v2 行为与 v1 一致（全量 MCP）
- `true`（prod 推荐）：必须补；缺失的 Skill 会在 SkillLoader 初始化时报 warning，运行时对该 Skill 降级为"无 MCP 工具"（Agent 只能从 SKILL.md 自我推理）

### Q6: 凭证从 `config.yaml` 移到 `.env` / docker secrets，历史仓库里的凭证怎么办？

A: 必须做 `git filter-repo` 清除历史。这是**不可回滚**操作——commit hash 整体变更，所有下游副本需配合处理。Phase 0 的"凭证轮换"是前置条件——旧凭证虽仍在历史里（clone 者本地），但已全部作废，无实际风险。

**v2.1 完整失效清单**（force-push 后以下全部失效/需重建）：
- CI/CD **缓存层**：GitHub Actions / GitLab CI 的构建 cache 按 commit sha 索引，全部失效 → 首次 build 时长翻倍
- **Docker layer cache**：registry 上按 base image + RUN 指令 sha 分层，如果 Dockerfile 被 filter-repo 改动过 sha（即使内容相同），本地/CI cache 命中率 0
- 所有 **开发者 clone**、**fork 仓库**、**LSP index**：必须重新 `clone`（不能 `pull`，会引发 merge conflict on rewritten history）
- 所有 **tag** 指向旧 commit → 需 `git push origin --tags --force` 重写，下游 `git pull --tags` 不会覆盖本地已有 tag，需显式 `git tag -d` 后再 fetch
- **PR/MR 关联**：开放中的 PR 基于旧 history，会变成 "unmergeable"，需 rebase 或重新提

**强制 48h 通告 SOP**：
1. `T-48h`：全员邮件/飞书通告计划 force-push 时间、理由、应对步骤
2. `T-24h`：二次提醒 + 列名未读用户
3. `T-0`：执行
4. `T+0 ~ T+24h`：值班支持下游重建

具体命令：

```bash
# 在 Phase 0 凭证轮换完成后，48h 通告届满
git filter-repo --path config.yaml --invert-paths

# 强制推送所有分支与 tag
git push origin --all --force
git push origin --tags --force

# 通告下游：所有 clone 者 & fork 需重新 clone；CI cache 预期命中率暂时 0
# 下游步骤：
#   git fetch --tags --prune --prune-tags   # 清理被删除的旧 tag
#   或直接：rm -rf 本地仓库 && git clone 新的
```

**不可回滚警示**：一旦推送，所有 clone 本地的 reflog、PR 基点、Git Actions run 历史都变"孤岛"。如需 undo，只能事先备份裸仓库 (`git clone --mirror`)。

### Q7: LRUCache(1000) 对高活跃场景会误驱逐吗？

A: 日活 1000 以下场景无影响。若有预期超 1000 的用户：
- 调大 `session.max_active_sessions`
- 监控 metric `xiaopaw_session_lock_evicted_total`，非 0 时报警

### Q8: v2 的 Qwen 官方 tokenizer 找不到怎么办？

A: 自动降级到 `token_counter_mode=rough`（len//2）。校准报告会告诉你偏差范围，生产上可接受的 rough 场景通常 p95 偏差 < 30%，对 45% 压缩阈值的影响是触发时机偏早 10-15%，非致命。

### Q9: 回滚到 v1 后 pgvector 里的 v2 新数据会有问题吗？

A: 不会。pgvector 表结构 v1 兼容 v2（约束只是加法），v2 写入的行 v1 能正常读。唯一需要注意的是 RLS（若启用），v1 回滚后要先 `DISABLE ROW LEVEL SECURITY`。

### Q10: 如何验证切换过程中没丢消息？

A: 切换前后各 5 分钟，对比 `xiaopaw_inbound_total` 指标与飞书开放平台后台的"事件推送成功数"。差值应 = 切换窗口秒数 × QPS。超出预期需查飞书后台的"推送失败重试"记录。

### Q11: 为什么 v2.1 取消了 `encrypt_key` 验签？

A: v2.0 文档假设 `lark_oapi.ws.Client(encrypt_key=..., verification_token=...)`，但 SDK 真实签名**不接受这两个参数**——详见 [sdk-verification-report.md §1](./sdk-verification-report.md)。

WebSocket 长连模式下，飞书服务端在建立长连时**已通过 app_secret 完成客户端身份验证**，消息加密/解密也由服务端与 SDK 握手时协商；业务代码无需也无法介入 HMAC 校验。

v2.1 把 T3 防御改为**应用层 ReplayCache**（`event_id → ts` LRU + 5min TTL）：
- Feature flag `enable_webhook_replay_cache`（原 `enable_webhook_signature` 改名）
- 实现位置：`xiaopaw/feishu/listener.py` `on_message` 入口
- 残余风险：单节点进程重启丢 cache，5 min 窗口内的旧 event 可重放——敏感场景可升级到 Redis `SET event_id 1 EX 300 NX`

若项目必须保留传统 `encrypt_key` HMAC 校验，只能走 **HTTP 回调路径**（另起 FastAPI endpoint + `EventDispatcherHandler.builder().encrypt_key(...)`），与 WS 模式二选一；凭证文件 `feishu_encrypt_key.txt` 作为该路径的备用占位保留。

---

## 11. 附：时间估算

### 11.1 单次蓝绿切换总工期

假设两人并行，按日均 8h 计：

| 阶段 | 时长 | 关键节点 |
|---|---|---|
| Rollout-P0（凭证轮换 / canary / tokenizer） | **3 自然日**（v2.1：含 force-push 48h 通告 + 凭证审批流程；原 1.5 天严重低估） | 凭证全部作废生效 |
| Rollout-P1（canary 启动） | 0.5 天 | canary 自检通过 |
| Rollout-P2（canary baseline） | 0.5 天 | 24h smoke 完成 |
| Rollout-P3（canary 72h 观测） | **72h 连续** | 内存 / 延迟 baseline |
| Rollout-P4（蓝绿切换） | ≤ 5 min（v2.1 含 WS 重连 5-30s + 冒烟） | 飞书 WebSocket 重连 |
| Rollout-P4 后稳定期 | 72h | 无回滚事件 |
| Rollout-P5（v1 下线） | 1 天 | 旧凭证作废、v1 归档 |
| **总工期** | **≈ 8 自然日**（v2.1 修正；3 + 1 工作日 + 72h × 2 观察 + 1 切换日） | |

### 11.2 停机窗口说明

| 窗口类型 | 时长 | 发生时机 |
|---|---|---|
| 飞书 WebSocket 重连（真实用户可感知） | 5-30s（v2.1 修正） | Rollout-P4 切换 |
| v2 容器启动 + healthcheck | 30-60s | Rollout-P4 期间并行（用户不可感知） |
| pgvector schema ALTER（小表直接 / 大表两阶段） | 小表 < 1s；大表 VALIDATE 30-60s 但并发 DML 不阻塞 | Rollout-P4 之前（canary 阶段做） |
| Cron tick 错过 | ≤ 1 次 tick 间隔（默认 60s） | Rollout-P4 切换 |
| 最长用户可感知停机 | **≤ 5 min**（v2.1 修正） | 蓝绿切换窗口 |

### 11.3 资源开销估算

相比 v1：

| 资源 | v1 | v2 | 差异 |
|---|---|---|---|
| 容器内存（稳态） | ~400MB | ~550MB | +150MB（Qwen tokenizer + LRUCache + trace） |
| 容器内存（峰值） | ~800MB | ~900MB | +100MB |
| 镜像大小 | ~1.2GB | ~1.4GB | +200MB（tenacity / filelock / tokenizer 依赖） |
| 冷启动时间 | ~3s | ~4s | +1s（tokenizer 惰性加载可在首次调用时摊销） |
| pgvector 写入延迟 | ~15ms | ~15ms | 不变 |
| LLM 调用延迟 | ~800ms | ~800ms | 不变 |

---

## 下一步阅读

- **详细数据兼容性 / ALTER 脚本** → [03-data.md §15](03-data.md)
- **凭证治理 / 威胁模型** → [07-security.md](07-security.md)
- **配置字段完整清单** → [09-config.md](09-config.md)
- **部署拓扑 / Docker compose** → [08-deployment.md](08-deployment.md)
- **切换后的监控告警** → [06-observability.md](06-observability.md)
