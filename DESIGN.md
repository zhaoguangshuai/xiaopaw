# XiaoPaw v2 详细设计文档（总纲）

- **项目**：XiaoPaw v2（小爪子 v2）— 飞书本地工作助手（生产加固版）
- **版本**：**v3**（2026-04-24 系统加固）/ v2.1（2026-04-19 升级）
- **原始日期**：2026-04-17（v2.0-draft）
- **改造依据**：
  - v1 Review 报告：4 CRITICAL + 10 HIGH + 11 MEDIUM + 3 LOW 缺陷清单
  - v2 重构设计文档：基于 4 路 sub-agent review 的方案
  - **v2.1 新增**：5 路文档 review 发现的 36 个问题 + Phase 0 SDK/并发验证报告
- **设计原则**：保留三层记忆架构（Bootstrap 预加载 / `@before_llm_call` / 四件套 / 文件系统记忆写通道 / pgvector 混合检索），叠加生产级加固。

---

## 📝 v2.0 → v2.1 变更日志（ADR-v2.1-001 ~ 010）

**Phase 0 验证后的重大修正**（详见 `sdk-verification-report.md` / `concurrency-verification-report.md`）：

| ADR | 修正 | 影响文档 |
|---|---|---|
| **ADR-v2.1-001** | `lark-oapi.ws.Client` **不支持** `encrypt_key` / `verification_token` 参数；WebSocket 模式验签由飞书服务端做。T3 防御改为**应用层 ReplayCache**（event_id LRU+TTL） | 07 / 04 / 02 / 09 |
| **ADR-v2.1-002** | Session 锁方案改为 **`_dispatch_lock` + LRUCache 两级锁**。承认 LRUCache 核心是防 OOM 不是防竞态 | 02 / 05 / 01 |
| **ADR-v2.1-003** | `psycopg_pool`（psycopg3）**不兼容** psycopg2；改用 `psycopg2.pool.ThreadedConnectionPool` | 04 / 09 / 11 |
| **ADR-v2.1-004** | `lark-oapi` 响应属性是 `.raw` 不是 `.raw_response`；全文替换 | 02 / 04 |
| **ADR-v2.1-005** | `run_in_executor` **任何 Python 版本都不自动 copy_context**（非 3.13 bug）；`to_thread` 自 3.9 起自动。shutdown 改用公开 `loop.shutdown_default_executor()` | 05 / 06 |
| **ADR-v2.1-006** | `@before_llm_call` 必须 **in-place 修改 messages**（`messages[:] = ...`）；`context.llm.context_window_size` 从 config 读固定值 | 02 |
| **ADR-v2.1-007** | 新增威胁 T8-T11（Cron→Runner 注入 / MCP 宿主暴露 / Cron payload 注入 / routing_key 伪造）；T1 残余风险从 MEDIUM 升 HIGH | 07 / 01 |
| **ADR-v2.1-008** | 5 张 SSOT 权威清单（`docs/ssot/`）：locks / tasks / ports / feature-flags / threats；其他文档引用不硬编码 | 所有 |
| **ADR-v2.1-009** | 端口统一：health+metrics 同端口 **8090**；TestAPI **9090**（显式 loopback bind）；sandbox 容器间 **8080** | 04 / 06 / 08 / 09 |
| **ADR-v2.1-010** | `save_session_ctx + append_session_raw` 双写拆分：MemoryAwareCrew 只返回 reply 和暴露 `_index_coroutine`，写动作统一在 Runner._handle | 02 / 05 |

**新增产出**：
- `docs/ssot/` — 5 张 SSOT 清单
- `docs/sdk-verification-report.md` — SDK 真相报告
- `docs/concurrency-verification-report.md` — 并发真相报告
- `docs/test-cases-for-known-risks.md` — 26 组已知风险测试用例
- `docs/iteration-v2.1-plan.md` — 迭代计划

---

## 📝 v2.1 → v3 变更日志（系统加固，2026-04-24 ~ 2026-05-02）

**设计文档**：`docs/12-hook-hardening.md`（v3.1-rc1，4-way Review + E2E 反向验证）

### 核心新增

| 变更 | 内容 | 影响文档 |
|---|---|---|
| **Hook 框架** | HookRegistry（dispatch/dispatch_gate）+ HookLoader（两层 YAML）+ CrewObservabilityAdapter（4→7 映射）| 12 / 02 / 01 |
| **5+2 事件体系** | BEFORE_TURN/BEFORE_LLM/BEFORE_TOOL_CALL/AFTER_TOOL_CALL/AFTER_TURN + TASK_COMPLETE/SESSION_END | 12 |
| **shared_hooks/ 加固层** | 9 个策略文件（1337 行），hooks.yaml 两段式配置，零业务代码修改 | 12 |
| **观测策略** | structured_log（82 行）+ langfuse_trace（779 行，含 span 栈 + auto-close + batch flush）| 12 / 06 |
| **安全策略** | sandbox_guard（107 行，正则输入消毒）+ permission_gate（75 行，三级控制）+ audit_logger（63 行）| 12 / 07 |
| **可靠性策略** | cost_guard（69 行，$1 预算围栏）+ loop_detector（50 行，阈值 3）+ retry_tracker（40 行）| 12 |
| **Runner 集成** | pending_deny 检查 + GuardrailDeny 捕获 + 7 事件 dispatch 调用点 | 02 / 05 |

### 测试

| 类型 | 数量 | 新增文件 |
|---|---|---|
| 单元测试 | 188（shared_hooks 106 + hook_framework 64 + v3_fixes 18）| tests/unit/shared_hooks/ + tests/unit/hook_framework/ |
| 集成测试 | 40（hook_chain / security_chain / adapter / two_layer_config / deny_flow / trace_quality / deny_observability）| tests/integration/ |
| E2E | 65 用例 / 15 场景 + 2 persona | tests/e2e/ |
| **合计** | **293** | |

### E2E 验证与修复

| 文档 | 发现 |
|---|---|
| `14-e2e-test-design.md` | 15 场景覆盖矩阵、2 层 client（slash_client / llm_client）、LLM-as-Judge 断言 |
| `15-e2e-fix-structured-log-and-timing.md` | 5 个问题：3 缺失 handler / duration_ms=0 / Langfuse init 静默失败 / tool_call 计数不匹配 / E2E-11 超时 |
| `langfuse-trace-fix-design.md` | 8 个 trace 质量问题（P1-P8）：AFTER_TURN 语义混淆、span 树 3 层嵌套预期 |
| `e2e-05-search-regression-report.md` | 搜索场景回归验证通过，185.46s，21 observations |
| `e2e-05-langfuse-trace-deep-analysis.md` | trace 质量审计：86% 有 input，62% 有 output，0% 有 token usage |

### 4-Way Review 结论（ADR-v3-001 ~ ADR-v3-008）

- **2 CRITICAL**：ADR-v3-001 全局 hook 并发安全 / ADR-v3-002 pending_deny 静默丢弃
- **6 HIGH**：ADR-v3-003 ~ 008（handler 异常隔离、策略注册顺序、cost 精度等）
- **8 MEDIUM**：见 `12-hook-hardening.md` §12

---

## 📖 文档导航

本文档为**总纲**，链接各专题子文档。阅读顺序建议：

| 优先级 | 文档 | 目标读者 | 核心回答 |
|---|---|---|---|
| ⭐⭐⭐ | 本文档 | 所有人 | 这个系统是什么？为什么存在？v1→v2 改了什么？ |
| ⭐⭐⭐ | [01-architecture.md](docs/01-architecture.md) | 架构师 / 新加入工程师 | 整体架构图、数据流、信任边界 |
| ⭐⭐⭐ | [02-modules.md](docs/02-modules.md) | 实现工程师 | 每个模块职责、接口、关键实现 |
| ⭐⭐ | [03-data.md](docs/03-data.md) | 实现工程师 / DBA | Session 存储、pgvector schema、ctx.json 格式 |
| ⭐⭐ | [04-api.md](docs/04-api.md) | 集成方 | 飞书接入、TestAPI、/metrics、/health |
| ⭐⭐⭐ | [05-concurrency.md](docs/05-concurrency.md) | 实现工程师 | 锁模型、队列、task 生命周期（v2 核心加固点） |
| ⭐⭐ | [06-observability.md](docs/06-observability.md) | SRE | trace_id、metrics、日志、告警 |
| ⭐⭐⭐ | [07-security.md](docs/07-security.md) | 安全工程师 / 运维 | 威胁模型、凭证管理、合规基线（v2 新增） |
| ⭐⭐ | [08-deployment.md](docs/08-deployment.md) | 运维 | Docker 部署、配置、健康检查、回滚 |
| ⭐⭐ | [09-config.md](docs/09-config.md) | 运维 / 实现 | config.yaml 字段、feature flags、env 优先级 |
| ⭐⭐ | [10-testing.md](docs/10-testing.md) | 实现工程师 / QA | 测试分层、覆盖率要求、故障注入 |
| ⭐ | [11-migration-v1-to-v2.md](docs/11-migration-v1-to-v2.md) | v1 既有部署方 | 从 v1 升级到 v2 的步骤 |
| ⭐⭐⭐ | [12-hook-hardening.md](docs/12-hook-hardening.md) | 架构师 / 安全 / SRE | **【v3 新增】** Hook 框架 + 可靠性策略 + 安全策略（模块五集成） |
| ⭐⭐ | [13-test-design-hook-hardening.md](docs/13-test-design-hook-hardening.md) | QA / 实现 | **【v3 新增】** 加固层测试设计（136 用例规格） |
| ⭐⭐ | [14-e2e-test-design.md](docs/14-e2e-test-design.md) | QA / 实现 | **【v3 新增】** E2E 测试设计（15 场景覆盖矩阵） |
| ⭐ | [15-e2e-fix-structured-log-and-timing.md](docs/15-e2e-fix-structured-log-and-timing.md) | 实现 | **【v3 新增】** E2E 发现的 5 个问题修复记录 |
| ⭐ | [langfuse-trace-fix-design.md](docs/langfuse-trace-fix-design.md) | 实现 / SRE | **【v3 新增】** Langfuse trace 质量 8 问题修复设计 |

### SSOT 权威清单（v2.1 新增，所有文档引用不硬编码）

| 清单 | 内容 |
|---|---|
| [`docs/ssot/locks.md`](docs/ssot/locks.md) | 所有锁（asyncio.Lock / Semaphore / filelock / LRUCache）× 资源 × 粒度 × 失败降级 |
| [`docs/ssot/tasks.md`](docs/ssot/tasks.md) | 所有 asyncio Task / 后台循环 / executor 任务 + shutdown 顺序 |
| [`docs/ssot/ports.md`](docs/ssot/ports.md) | 所有端口（8090 / 9090 / 8080 / 5432）及其鉴权 |
| [`docs/ssot/feature-flags.md`](docs/ssot/feature-flags.md) | 12 个 feature flag × 默认值 × 对应缺陷 × 回滚风险 |
| [`docs/ssot/threats.md`](docs/ssot/threats.md) | T1-T11 威胁 × STRIDE × 防御层 × 残余风险 × 测试锚点 |

### Phase 0 专题报告（v2.1 新增）

| 文档 | 内容 |
|---|---|
| [`docs/sdk-verification-report.md`](docs/sdk-verification-report.md) | lark-oapi / CrewAI / psycopg / 飞书错误码真相验证 |
| [`docs/concurrency-verification-report.md`](docs/concurrency-verification-report.md) | LRUCache / copy_context / shutdown / filelock 并发行为验证 |
| [`docs/test-cases-for-known-risks.md`](docs/test-cases-for-known-risks.md) | 26 组针对已知风险的测试用例（P0/P1/P2/P3）+ CI gate 补丁表 |
| [`docs/iteration-v2.1-plan.md`](docs/iteration-v2.1-plan.md) | v2.0 → v2.1 迭代计划 + 36 问题修订路线 |

---

## 1. 项目概述

### 1.1 定位

**XiaoPaw v2**（小爪子 v2）是飞书本地 AI 助手，采用三层记忆架构与生产级加固：
- **架构**：飞书本地助手（WebSocket 长连接、无需公网 IP、Skills 生态、AIO-Sandbox 执行隔离）
- **记忆系统**：上下文层（Bootstrap） / 文件层（workspace） / 搜索层（pgvector）
- **加固**：并发安全、容错降级、可观测、安全合规、测试覆盖

### 1.2 v1 vs v2 核心差异

| 维度 | v1（初始版本） | v2（生产加固） |
|---|---|---|
| 凭证 | dummy 值随 `config.yaml` 入库 | `.env` 注入 + 正则+hash 安全校验 |
| Session 锁 | `dict` 永驻（长跑 OOM） | `LRUCache(1000)` 自然淘汰 |
| 索引 task | `create_task` 局部变量（Py3.12+ GC 风险） | Runner 集合托管 + `add_done_callback` |
| 搜索隔离 | `routing_key` 可空（跨用户泄露） | 三层强制（脚本 required + Skill required + SkillLoader 校验拒绝覆盖） |
| Token 计数 | `len//2` 粗估（偏差 25%） | Qwen 官方 tokenizer（首选）+ rough 降级 |
| Skill 超时 | 无（Sub-Crew 卡死阻塞队列） | `asyncio.wait_for` + 主动 sandbox kill |
| 飞书限流 | 固定退避不识别 429 | 真实错误码 99991663/99991672 + HTTP 层 429 |
| Cron 存储 | 单进程原子写 | `filelock` 跨进程（单节点）/ PG advisory lock（多节点） |
| memory-save 并发 | 无锁（最后写入覆盖） | Topic 文件锁 + 超时报错 |
| 观测 | logger INFO + `/metrics` 无鉴权 | trace_id 贯穿 + Bearer Token + 8 核心指标 |
| 威胁模型 | 未讨论 | MCP 白名单 + memory 投毒过滤 + webhook 验签 + 入站速率限制 |
| 合规 | 未讨论 | PII 脱敏 + 数据本地化披露 + 日志留存 + 凭证轮换 runbook |
| 部署 | `:latest` 无 healthcheck | tag+digest + healthcheck + `USER nobody` |
| 测试 | 642 单元，86% cov，4 个失败 | ≥720 单元，88% cov（模块级 ≥90%），4 失败清零 |
| CI/CD | 无 | Actions + pre-commit + pip-audit(HIGH fail) + trace 覆盖率 gate |

### 1.3 部署形态

```
[用户/运维]          [XiaoPaw v2 主进程]             [外部依赖]
   │                        │                           │
   │   飞书 WebSocket (长连) │                           │
   ├──────────────────────▶ │                           │
   │                        │   Qwen API (DashScope)    │
   │                        ├──────────────────────────▶│
   │                        │   百度千帆 API             │
   │                        ├──────────────────────────▶│
   │                        │   pgvector（单节点内网）   │
   │                        ├──────────────────────────▶│
   │                        │   AIO-Sandbox (Docker)    │
   │                        ├──────────────────────────▶│
   │   Prometheus 拉取指标   │                           │
   ├──────────────────────▶ │                           │
```

**v2 单节点部署前提**（见 §2.4）：本次加固不支持多副本/多节点。多节点需等 M4 阶段规划。

### 1.4 非目标（Non-Goals）

v2 明确**不做**的事，避免范围蠕变：

- ❌ 换掉 CrewAI / Qwen（核心运行时依赖）
- ❌ 换掉 pgvector 为 Milvus / Qdrant（验证过的 PG 方案）
- ❌ 改记忆写通道为数据库（文件系统记忆架构选择）
- ❌ 多 Agent 协调 / 多节点部署 / 跨副本并发（M4 范畴）
- ❌ 替换 aiohttp 为 FastAPI（无必要）
- ❌ 流式响应（飞书卡片 UI 限制）

---

## 2. 系统架构（摘要）

> 详细架构图、数据流时序、信任边界图见 [01-architecture.md](docs/01-architecture.md)。

### 2.1 核心概念

**消息流**：
```
飞书 WebSocket ─▶ FeishuListener ─▶ SessionRouter (routing_key)
                                        ▼
                                    Runner (per-routing_key 串行队列)
                                        ▼
              ┌──── slash command 拦截 ────┐
              │ /new /verbose /help /status │
              └─────────────────────────────┘
                                        ▼
                              MemoryAwareCrew
                              ├── @before_llm_call
                              │    ├── Bootstrap（首次）
                              │    ├── prune_tool_results
                              │    └── maybe_compress
                              └── SkillLoaderTool
                                   ├── reference skill → 返回 SKILL.md
                                   └── task skill → Sub-Crew (Docker sandbox MCP)
                                        ▼
                              FeishuSender / CaptureSender
                                        ▼
                               飞书卡片 / TestAPI 响应
```

**路由键三类**：
- `p2p:{open_id}`（私聊）
- `group:{chat_id}`（群聊）
- `thread:{chat_id}:{thread_id}`（话题）

**Skill 两型**：
- **reference**：SKILL.md 内容返回 Main Agent 自我推理
- **task**：派生隔离 Sub-Crew，接 AIO-Sandbox MCP（`sandbox_execute_bash` / `_code` / `_file_operations` / `browser_*` 等）
- **【v2 新增】MCP tool 白名单**：生产模式下 Sub-Crew 仅暴露 Skill 声明的 `allowed_tools`（见 [07-security.md §2](docs/07-security.md)）

**三层记忆**：
- **上下文层**：`MemoryAwareCrew @CrewBase` + `@before_llm_call` hook
- **文件层**：`memory-save` / `skill-creator` / `memory-governance` Skills
- **搜索层**：`search_memory` Skill + pgvector 异步入库、混合检索

### 2.2 系统边界

XiaoPaw v2 **包含**：
- 主进程（FeishuListener + Runner + Agent + CronService + CleanupService + TestAPI + metrics）
- pgvector 数据库（作为依赖组件，生产独立部署）
- AIO-Sandbox 容器（作为依赖组件，独立部署）

**不包含**（外部依赖）：
- 飞书开放平台（App / WebSocket 服务）
- Qwen API（阿里云 DashScope）
- 百度千帆 API
- Prometheus / Grafana（选配）

### 2.3 信任边界（v2 新增核心）

```
[Untrusted]                          [Semi-Trusted]                       [Trusted]
飞书 Webhook  ─(签名校验)─▶ FeishuListener ─(内部队列)─▶ Runner
                                                             │
用户 Test API  ─(Bearer Token)─▶ TestAPI ──────────────────▶│
                                                             │
                                                             ▼
                                                       MemoryAwareCrew
                                                             │
                                              (MCP tool 白名单)
                                                             │
                                                             ▼
                                                       Sub-Crew (Sandbox)
                                                             │
                                              (路径遍历防护 + 非 root)
                                                             │
                                              ┌──────────────┴──────────────┐
                                              ▼                              ▼
                                       pgvector (权限最小化)            workspace 目录
                                              (RLS / 按 routing_key 隔离) (mount 精确到 session)
```

**信任边界规则**：
1. **未信任侧**（飞书 Webhook / TestAPI 用户输入）：**必须**经过签名/token 校验才能流入内部
2. **半信任侧**（Runner / Agent）：执行业务逻辑；不直接接外网
3. **信任侧**（pgvector / workspace）：仅允许经过鉴权的内部请求访问

---

## 3. 目录结构（v2）

```
xiaopaw-v2/
├── README.md                        # 项目概览 + 快速开始
├── DESIGN.md                        # 本文档（总纲）
├── config.yaml.example              # 配置模板（checked-in）
├── .env.example                     # 凭证模板（checked-in）
├── pyproject.toml
├── requirements.txt                 # 版本锁定（~=x.y.z）
├── pgvector-docker-compose.yaml     # pgvector 部署
├── sandbox-docker-compose.yaml      # AIO-Sandbox 部署
├── xiaopaw-docker-compose.yaml      # 【v2 新增】主服务 compose
├── Dockerfile                       # 【v2 新增】USER nobody + multi-stage
├── schema.sql                       # pgvector 表结构
│
├── docs/                            # 详细设计（本总纲链接）
│   ├── 01-architecture.md
│   ├── 02-modules.md
│   ├── 03-data.md
│   ├── 04-api.md
│   ├── 05-concurrency.md            # 【v2 新增】
│   ├── 06-observability.md
│   ├── 07-security.md               # 【v2 新增】
│   ├── 08-deployment.md             # 【v2 新增】
│   ├── 09-config.md                 # 【v2 新增】
│   ├── 10-testing.md
│   ├── 11-migration-v1-to-v2.md     # 【v2 新增】
│   ├── 12-hook-hardening.md         # 【v3 新增】Hook 框架 + 加固策略设计
│   ├── 13-test-design-hook-hardening.md  # 【v3 新增】加固层测试设计
│   ├── 14-e2e-test-design.md        # 【v3 新增】E2E 测试设计
│   ├── 15-e2e-fix-structured-log-and-timing.md  # 【v3 新增】E2E 修复记录
│   ├── langfuse-trace-fix-design.md # 【v3 新增】Langfuse trace 质量修复
│   ├── e2e-05-*.md                  # 【v3 新增】E2E 分析报告
│   ├── threat-model.md              # 【v2 新增】威胁模型
│   ├── compliance-baseline.md       # 【v2 新增】合规基线
│   ├── secret-rotation-runbook.md   # 【v2 新增】凭证轮换 runbook
│   ├── phase0-checklist.md          # 【v2 新增】Phase 0 清单
│   └── tokenizer-calibration.md     # 【v2 新增】Tokenizer 校准报告
│
├── xiaopaw/                         # 主代码
│   ├── main.py                      # 入口
│   ├── models.py                    # InboundMessage / Attachment / SenderProtocol
│   ├── runner.py                    # Runner（per-routing_key 队列 + gen counter）
│   │
│   ├── config/                      # 【v2 新增】配置模块
│   │   ├── validator.py             # Pydantic schema 校验
│   │   ├── safety.py                # 凭证安全校验（正则+hash）
│   │   └── flags.py                 # FeatureFlags registry
│   │
│   ├── feishu/
│   │   ├── listener.py              # WebSocket 事件 → InboundMessage（含验签）
│   │   ├── downloader.py
│   │   ├── sender.py                # 真实错误码 + HTTP 429 + Semaphore 并发控
│   │   └── session_key.py
│   │
│   ├── api/
│   │   ├── test_server.py           # Bearer Token + loopback 双重防护
│   │   ├── capture_sender.py
│   │   └── schemas.py
│   │
│   ├── agents/
│   │   ├── main_crew.py             # MemoryAwareCrew（crew 仅暴露 _index_coroutine）
│   │   ├── skill_crew.py
│   │   ├── models.py
│   │   └── config/{agents,tasks}.yaml
│   │
│   ├── memory/
│   │   ├── bootstrap.py             # Bootstrap 四件套
│   │   ├── context_mgmt.py          # prune / compress / ctx.json
│   │   ├── token_counter.py         # 【v2 新增】Qwen tokenizer（惰性）
│   │   ├── indexer.py               # @cache 单例
│   │   └── config.py                # 阈值常量统一
│   │
│   ├── tools/
│   │   ├── skill_loader.py          # MCP tool 白名单 + wait_for 超时
│   │   ├── add_image_tool_local.py
│   │   ├── baidu_search_tool.py
│   │   └── intermediate_tool.py
│   │
│   ├── llm/
│   │   └── aliyun_llm.py            # structured marker JSON（替代字符串）
│   │
│   ├── session/
│   │   ├── manager.py               # LRUCache + asyncio.to_thread 倒序读
│   │   └── models.py
│   │
│   ├── cron/
│   │   ├── service.py               # filelock + DLQ
│   │   ├── storage.py               # 【v2 新增】跨进程锁封装
│   │   └── models.py
│   │
│   ├── cleanup/
│   │   └── service.py
│   │
│   ├── observability/
│   │   ├── logging_config.py        # JSON log + caller + stacktrace
│   │   ├── trace.py                 # 【v2 新增】ContextVar + executor helper
│   │   ├── pii_mask.py              # 【v2 新增】手机/邮箱/身份证脱敏
│   │   ├── security.py              # 【v2 新增】RateLimiter + memory-save filter
│   │   ├── metrics.py               # 8 个核心指标
│   │   └── metrics_server.py        # Bearer + constant_time_equals
│   │
│   ├── utils/
│   │   └── retry.py                 # 【v2 新增】tenacity(tuple) 工厂
│   │
│   ├── hook_framework/              # 【v3 新增】Hook 框架（592 行）
│   │   ├── registry.py              # HookRegistry：dispatch + dispatch_gate（118 行）
│   │   ├── loader.py                # HookLoader：YAML 两层配置 + 依赖注入（197 行）
│   │   └── crew_adapter.py          # CrewObservabilityAdapter：4→7 映射 + pending_deny（274 行）
│   │
│   └── skills/                      # 保留 v1 的 13 个 Skills（MCP 白名单由 SKILL.md frontmatter 声明）
│       ├── pdf/ docx/ pptx/ xlsx/
│       ├── feishu_ops/ scheduler_mgr/ baidu_search/ web_browse/
│       ├── history_reader/
│       ├── memory-save/             # 含 BLOCKED_PATTERNS 过滤
│       ├── skill-creator/           # 含路径遍历防护
│       ├── memory-governance/
│       └── search_memory/           # routing_key required
│
├── shared_hooks/                    # 【v3 新增】加固层（1337 行，零业务代码修改）
│   ├── hooks.yaml                   # 两段式配置入口（72 行）
│   ├── structured_log.py            # JSON 事件日志（82 行）
│   ├── langfuse_trace.py            # Langfuse trace/span/generation 全链路（779 行）
│   ├── audit_logger.py              # JSONL 审计日志（63 行）
│   ├── sandbox_guard.py             # 输入消毒：路径穿越/shell/prompt（107 行）
│   ├── permission_gate.py           # 工具权限 deny/warn/allow（75 行）
│   ├── cost_guard.py                # 成本围栏 $1 预算（69 行）
│   ├── loop_detector.py             # 循环检测阈值 3（50 行）
│   └── retry_tracker.py             # 重试追踪最大 5 次（40 行）
│
├── workspace-init/                  # 初始化模板（soul/user/agent/memory.md）
│
├── tests/                           # 【v3 大幅扩充】293 用例
│   ├── unit/                        # 188 用例（shared_hooks 106 + hook_framework 64 + v3_fixes 18）
│   ├── integration/                 # 40 用例（hook_chain / security_chain / deny_flow 等）
│   ├── e2e/                         # 65 用例（15 场景 + 2 persona）
│   └── fixtures/                    # hook_tool_inputs / hook_yaml_samples / security_policy_samples
│
├── scripts/                         # 【v2 新增】CI 脚本
│   ├── verify_trace_coverage.py
│   ├── verify_pii_masking.py
│   └── preflight_check.py
│
├── .github/
│   └── workflows/ci.yml             # 【v2 新增】
├── .pre-commit-config.yaml          # 【v2 新增】
└── .gitignore                       # config.yaml / .env / data/
```

---

## 4. 模块概览

> 每个模块详细设计见 [02-modules.md](docs/02-modules.md)。

| 模块 | v2 变化 | 关键约束 |
|---|---|---|
| `FeishuListener` | 加入 webhook 验签 + 入站速率限制 | 每用户每分钟 ≤20 条 |
| `SessionRouter` | 不变 | 纯函数 |
| `Runner` | queue_gen counter + `_pending_index_tasks` 托管 | 同 routing_key 串行、不同并行 |
| `SessionManager` | LRUCache(1000) + `asyncio.to_thread` 流式倒序读 | JSONL append-only，meta 首行 |
| `MemoryAwareCrew` | `_index_coroutine` 不再自己 `create_task` | `@before_llm_call` hook 保持不变 |
| `SkillLoaderTool` | MCP tool 白名单 + `asyncio.wait_for` | Skill 超时默认 120s |
| `memory/bootstrap` | 不变 | memory.md ≤200 行（统一常量） |
| `memory/context_mgmt` | 压缩 cutoff 保护 tool_calls pair | 45% 触发阈值不变 |
| `memory/token_counter` | 【v2 新增】Qwen tokenizer 惰性 | Phase 0 校准报告支撑 |
| `memory/indexer` | `@cache` 单例 client | `async_index_turn` 签名不变 |
| `FeishuSender` | 真实 429 错误码 + Semaphore | 最大并发 5 |
| `CronService` | filelock + DLQ + 不推 next_run | 单节点 |
| `memory-save` Skill | BLOCKED_PATTERNS 过滤 + topic 锁 | 内容长度 ≤2000 |
| `search_memory` Skill | routing_key required + 校验拒绝 | 不得跨 routing_key 查询 |
| `/metrics` 服务 | Bearer Token + constant_time_equals | prod 强制启用 |
| `TestAPI` | Bearer Token + loopback bind | prod 禁用 |
| `observability/trace` | 【v2 新增】ContextVar + executor helper | 覆盖率 ≥85% |
| `observability/pii_mask` | 【v2 新增】落盘前 mask 手机/邮箱/身份证 | 所有 user_message 日志 |
| `observability/security` | 【v2 新增】RateLimiter + memory-save filter | 入口强制 |
| `hook_framework/registry` | 【v3 新增】HookRegistry（dispatch + dispatch_gate）| handler 异常不扩散（try-except 隔离）|
| `hook_framework/loader` | 【v3 新增】HookLoader（两层 YAML + deps 注入）| 声明顺序 = 实例化顺序 |
| `hook_framework/crew_adapter` | 【v3 新增】CrewObservabilityAdapter（4→7 事件映射）| pending_deny 机制绕过 CrewAI 异常吞噬 |
| `shared_hooks/structured_log` | 【v3 新增】JSON 结构化事件日志 | 零依赖降级（Langfuse 挂了还有日志）|
| `shared_hooks/langfuse_trace` | 【v3 新增】Langfuse trace/span/generation + batch flush | REST API v4 + span 栈 + auto-close |
| `shared_hooks/sandbox_guard` | 【v3 新增】正则输入消毒（路径穿越/shell/prompt 注入）| 确定性检测，不依赖 LLM |
| `shared_hooks/permission_gate` | 【v3 新增】工具权限三级控制 | Deny > Ask > Allow，YAML 声明 |
| `shared_hooks/cost_guard` | 【v3 新增】成本围栏（$1 预算）| AFTER_TURN 算账 + BEFORE_TOOL_CALL 拦截 |
| `shared_hooks/loop_detector` | 【v3 新增】循环检测（阈值 3）| MD5 哈希双路径去重 |
| `shared_hooks/retry_tracker` | 【v3 新增】重试追踪（最大 5 次）| 纯观测不阻断 |
| `shared_hooks/audit_logger` | 【v3 新增】JSONL 安全审计日志 | SESSION_END 写摘要 |

---

## 5. 数据概览

> 详细 schema 见 [03-data.md](docs/03-data.md)。

| 数据 | 位置 | 格式 | 生命周期 |
|---|---|---|---|
| Session index | `data/sessions/index.json` | JSON（write-then-rename 原子） | 永久（CleanupService 按策略清理） |
| Conversation history | `data/sessions/{sid}.jsonl` | JSONL（meta 首行 + 消息） | 180 天后归档冷存储 |
| ctx snapshot | `data/ctx/{sid}_ctx.json` | JSON（压缩快照） | 随 session 删除 |
| Raw audit log | `data/ctx/{sid}_raw.jsonl` | JSONL append-only | 30 天 |
| Traces | `data/traces/{sid}/{ts}_{msg_id}/` | meta.json + main.jsonl + skills/*.jsonl | 30 天 |
| Cron jobs | `data/cron/tasks.json` | JSON（filelock 保护） | 永久 |
| Cron DLQ | `data/cron/tasks.dlq.jsonl` | JSONL append-only | 永久（告警触发后人工处理） |
| pgvector | `memories` 表 | dual-vector + BM25 + tags | 180 天后归档 |
| Workspace | `data/workspace/sessions/{sid}/` | 用户文件 | 随 session 删除 |
| Workspace config | `data/workspace/.config/{feishu,baidu}.json` | JSON（mode 0600） | 启动时重写 |
| Feature flag 状态 | `config.yaml.feature_flags` | YAML | 配置生命周期 |

---

## 6. 接口概览

> 详细 API 见 [04-api.md](docs/04-api.md)。

| 接口 | 类型 | 鉴权 | 默认可用环境 |
|---|---|---|---|
| 飞书 `im.message.receive_v1` webhook | 入站 | 飞书签名 + Allowed chats 白名单 | 所有 |
| 飞书 `im.chat.member.bot.added_v1` | 入站 | 飞书签名 | 所有 |
| 飞书 REST（发消息/读文档/表格/日历） | 出站 | App Token | 所有 |
| TestAPI `POST /api/test/message` | 入站 | Bearer Token + 127.0.0.1 bind | dev（prod 强制关） |
| TestAPI `POST /api/test/clear` | 入站 | 同上 | dev |
| `/metrics` | 入站 | Bearer Token + constant_time | 所有（prod 强制 token） |
| `/health` | 入站 | 无 | 所有（仅返回 200 + git sha） |
| Qwen API（chat + embedding） | 出站 | API Key | 所有 |
| 百度千帆 `web_search` | 出站 | API Key | 可选 |
| pgvector PG 协议 | 出站 | user+password | 所有 |
| AIO-Sandbox MCP | 出站 | 无（内网） | 所有 |

---

## 7. 并发与锁（v2 核心加固）

> 详细设计见 [05-concurrency.md](docs/05-concurrency.md)。

**v2 锁清单**（所有锁均为**单进程/单节点**粒度）：

| 锁 | 类型 | 保护的资源 | 粒度 | 失败降级 |
|---|---|---|---|---|
| `Runner._dispatch_lock` | `asyncio.Lock` | `_queues` / `_workers` / `_queue_gen` 字典修改 | 全局 | 阻塞等待 |
| `SessionManager._index_lock` | `asyncio.Lock` | `index.json` 读写 | 全局 | 阻塞等待 |
| `SessionManager._jsonl_locks[sid]` | `asyncio.Lock`（LRU） | `{sid}.jsonl` append | 每 session | 阻塞等待 |
| `FeishuSender._sem` | `asyncio.Semaphore(5)` | 飞书 API 并发 | 全局 | 等信号量 |
| Cron tasks.json | `filelock.FileLock` | `tasks.json` 读写 | 单文件 | 超时 10s 报错 |
| memory-save topic | `filelock.FileLock` | `{topic}.md` 读写 | 每 topic | 超时 10s 报错 |

**task 生命周期**：
| Task | 持有者 | 回收机制 |
|---|---|---|
| Runner worker | `Runner._workers[key]` | idle timeout / 异常 finally 清理（比较 gen） |
| async_index_turn | `Runner._pending_index_tasks` set | `add_done_callback(self._pending_index_tasks.discard)` |
| CronService loop | `CronService._main_task` | shutdown 时 `cancel()` + `await` |
| metrics_server | `AppRunner` | shutdown `cleanup()` |

---

## 8. 可观测性

> 详细见 [06-observability.md](docs/06-observability.md)。

**三大支柱**：

1. **结构化日志**（JSON，含 `trace_id` / `routing_key` / `session_id` / `caller` / `stacktrace`）
2. **指标**（8 个核心，见下）
3. **Trace**（ContextVar 贯穿入口 → LLM → Skill → 出站）

**8 个核心指标**：
```
xiaopaw_inbound_total{source, routing_type}          # 入站消息
xiaopaw_llm_calls_total{model, status}               # LLM 调用
xiaopaw_agent_latency_seconds                        # 端到端耗时
xiaopaw_llm_latency_seconds{model}                   # LLM 耗时
xiaopaw_external_api_retry_total{api}                # 重试次数
xiaopaw_skill_timeout_total{skill}                   # Skill 超时
xiaopaw_feishu_rate_limit_total                      # 飞书限流命中
xiaopaw_cron_dlq_total                               # Cron 死信
```

---

## 9. 安全设计（v2 新增核心）

> 详细威胁模型见 [07-security.md](docs/07-security.md) / [threat-model.md](docs/threat-model.md)。

**7 大威胁**（每条都有对应防御层）：

| 威胁 | 防御 |
|---|---|
| T1 Prompt Injection → sandbox 逃逸 | Skill 级 MCP tool 白名单 + sandbox seccomp |
| T2 Memory Poisoning | memory-save BLOCKED_PATTERNS + H1 三层防护 |
| T3 飞书 Webhook 伪造 | `encrypt_key` + `verification_token` 验签 + 重放防护 |
| T4 凭证泄露 | Phase 0 强制轮换 + secret manager + `FORBIDDEN_DEFAULTS` 正则+hash |
| T5 Sub-Crew 路径遍历 | workspace mount 精确到 `{sid}/` 子目录 + `resolve()` 越界校验 |
| T6 SKILL.md YAML 注入 | `yaml.safe_load` 强制 + 路径白名单 |
| T7 DoS（消息洪水） | FeishuListener 入站速率限制（每用户 20/分钟） |

---

## 10. 合规基线（v2 新增）

> 详细见 [compliance-baseline.md](docs/compliance-baseline.md)。

- **PII 脱敏**：日志落盘前正则 mask（手机号 `1x********y`、邮箱 `***@***`、身份证 `xxxxxx********xxxx`）
- **数据本地化披露**：Qwen API / 百度搜索均为外发，README 明示企业合规评估要求
- **日志留存**：session JSONL 180 天 → 冷存储；trace 30 天；raw audit 30 天
- **数据主体权利**：提供导出 / 删除接口（PIPL）
- **容器非 root**：`USER nobody`
- **凭证轮换 runbook**：每 90 天 + 事件驱动 + 人员变动

---

## 11. 测试策略

> 详细见 [10-testing.md](docs/10-testing.md)。

**测试分层**（v3 更新，293 用例）：
- **单元**（188 用例）：shared_hooks 106 + hook_framework 64 + v3_fixes 18
- **集成**（40 用例）：hook_chain / security_chain / adapter / two_layer_config / deny_flow / trace_quality / deny_observability
- **E2E**（65 用例 / 15 场景 + 2 persona）
- **故障注入**（5 组）：ENOSPC / LLM 5xx / pgvector down / Skill 卡死 / 飞书 429
- **安全**（含于 E2E + unit）：sandbox_guard 18 用例 / permission_gate 10 用例 / E2E-13~15 安全场景
- **互斥正确性**（1 组）：100 并发 append 同 sid 无交叉

**覆盖率门**：全局 ≥88%，安全关键模块（sandbox_guard, permission_gate）≥95%，其他核心模块 ≥90%。

**CI gates**：
- ruff / black / bandit
- pytest --cov-fail-under=88
- 模块级 coverage 按文件 fail-under=90（安全模块 95）
- trace_id 覆盖率 ≥85%
- PII mask 验证
- pip-audit（HIGH fail）
- 【v3 新增】hook handler 注册完整性检查
- 【v3 新增】E2E Langfuse trace 质量门

---

## 12. 部署与运维

> 详细见 [08-deployment.md](docs/08-deployment.md)。

**部署形态**：
- **开发**：`docker compose up` 拉 xiaopaw + pgvector + sandbox 三容器，TestAPI 启用，Bearer Token 从 `.env.dev`
- **Canary**：Phase 0 就绪的独立环境，跑 72h 内存 baseline 监控
- **Prod**：单节点，`XIAOPAW_ENV=prod` 触发 `assert_production_safe`，TestAPI 强制关，`/metrics` Bearer Token 必填

**健康检查**：
- XiaoPaw 主服务：`GET /health` → 200 + git sha
- pgvector：内置 compose healthcheck
- AIO-Sandbox：`GET /healthz`（compose 内置）

**升级路径**：
- 配置变更 → SIGHUP 触发 config reload（feature flags 生效）
- 代码变更 → 滚动重启（飞书 WebSocket 会重连，队列中消息因 `_dispatch_lock` 不丢失）
- Schema 变更 → `docs/migration-v1-to-v2.md` 描述每个 migration step

---

## 13. 配置管理

> 详细见 [09-config.md](docs/09-config.md)。

**配置优先级**（高 → 低）：
1. 命令行参数（`--config path`）
2. 环境变量（`XIAOPAW_*`）
3. `config.yaml`（不入库）
4. `config.yaml.example`（仅作模板）

**凭证分层**（见 [07-security.md](docs/07-security.md)）：
- 明文 NEVER 进 git（`.env` 在 `.gitignore`）
- `.env.example` 仅作 key 名清单，**无任何值**
- 生产通过 secret manager 注入（Vault / K8s Secret / 阿里云 KMS）

**Feature Flags**（见 [09-config.md §Feature Flags](docs/09-config.md)）：
```yaml
feature_flags:
  token_counter_mode: "qwen_official"       # qwen_official / rough
  enable_skill_timeout: true                # H2
  enable_cron_filelock: true                # H8
  enable_memory_save_filelock: true         # H9
  enable_feishu_rate_limit_aware: true      # H6
  enable_trace_id: true                     # 横切
  enable_mcp_whitelist: true                # T1 MCP 白名单
  enable_memory_save_filter: true           # T2
  enable_webhook_signature: true            # T3
  enable_inbound_rate_limit: true           # T7
```

每个 flag 对应 metric `xiaopaw_feature_flag{name, enabled}`。

---

## 14. 迁移指南

> 详细见 [11-migration-v1-to-v2.md](docs/11-migration-v1-to-v2.md)。

**v1 → v2 零停机升级路径**（概要）：

1. **Phase 0 准备**：凭证全部轮换 + canary 就绪 + Tokenizer 校准报告
2. **蓝绿部署**：v2 服务独立启动 + pgvector schema migration（幂等 `CREATE TABLE IF NOT EXISTS`）
3. **数据迁移**：workspace / sessions 文件可直接复制（格式兼容）
4. **切流量**：飞书 WebSocket 一次性切到 v2（短暂重连窗口）
5. **验收**：72h canary 验证后下线 v1

---

## 15. 验收标准（v2 G1-G7）

> 量化指标均可在 CI/监控中自动验证。

- **G1** 消除 4 个 Blocker；canary 72h 内存增长斜率 <1MB/h
- **G2** 10 个 HIGH 缺陷全清；核心模块覆盖 ≥90%
- **G3** trace_id 覆盖 ≥85%；8 核心指标齐全
- **G4** 5 种故障注入进程存活 + 恢复后队列继续消费
- **G5** 单元 ≥720，全局 cov ≥88%，4 失败用例清零
- **G6** 威胁模型文档完成；Prompt Injection / Memory Poisoning 至少各 1 测试；凭证轮换 runbook 就绪
- **G7** PII 脱敏覆盖；数据本地化披露；日志留存策略明确；飞书入站速率限制生效

---

## 16. 文档交叉索引

```
DESIGN.md（本文）
 ├── 概述/架构 → 01-architecture.md
 ├── 模块细节 → 02-modules.md
 ├── 数据格式 → 03-data.md
 ├── 接口定义 → 04-api.md
 ├── 并发锁   → 05-concurrency.md
 ├── 观测    → 06-observability.md
 ├── 安全    → 07-security.md ← threat-model.md / secret-rotation-runbook.md
 ├── 部署    → 08-deployment.md
 ├── 配置    → 09-config.md
 ├── 测试    → 10-testing.md
 ├── 迁移    → 11-migration-v1-to-v2.md
 ├── Hook加固 → 12-hook-hardening.md       ← 【v3 新增】模块五三层集成
 ├── 加固测试 → 13-test-design-hook-hardening.md  ← 【v3 新增】136 用例规格
 ├── E2E设计  → 14-e2e-test-design.md       ← 【v3 新增】15 场景覆盖矩阵
 ├── E2E修复  → 15-e2e-fix-structured-log-and-timing.md  ← 【v3 新增】
 └── Trace修复 → langfuse-trace-fix-design.md  ← 【v3 新增】

运维专题:
 ├── Phase 0   → phase0-checklist.md
 ├── Tokenizer → tokenizer-calibration.md
 └── 合规     → compliance-baseline.md

E2E 分析报告（v3 新增）:
 ├── e2e-05-search-regression-report.md
 └── e2e-05-langfuse-trace-deep-analysis.md
```

---

## 17. 文档版本与贡献

- **v3.0**（2026-04-24）：系统加固 — Hook 框架 + shared_hooks 加固层（观测/可靠性/安全）+ 293 测试
- **v2.1**（2026-04-19）：Phase 0 验证 — 10 个 ADR 修正 + 5 张 SSOT 清单
- **v2.0-draft**（2026-04-17）：首版发布，基于 v1 review 结论和 v2 重构设计
- 每次重大变更需同步更新：
  - 本 DESIGN.md 的摘要
  - 对应子文档
  - `11-migration-v1-to-v2.md` 的里程碑
- 所有代码变更必须有对应设计文档改动（PR 模板检查）

---

**开始使用 v2**：
1. 阅读本文档 §1–§4 建立整体认知
2. 依次看 [01-architecture.md](docs/01-architecture.md) → [02-modules.md](docs/02-modules.md) 理解实现
3. 实施工程师必看 [05-concurrency.md](docs/05-concurrency.md)
4. 安全 / 运维必看 [07-security.md](docs/07-security.md)
5. 按 [phase0-checklist.md](docs/phase0-checklist.md) 启动 Phase 0
