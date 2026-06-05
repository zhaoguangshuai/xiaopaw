# XiaoPaw v2 — 飞书 AI 工作助手

XiaoPaw 是一个运行在飞书里的 AI 工作助手。你在飞书里发消息给它，它通过两层 Agent 架构理解意图、调用技能、在沙箱里执行代码，然后把结果返回给你。

```
飞书消息 → FeishuListener(WebSocket) → Runner(队列) → Main Crew(编排)
                                                          ↓
                                                    SkillLoaderTool
                                                          ↓
                                                    Sub-Crew(沙箱执行)
                                                          ↓
                                                    AIO-Sandbox(Docker/MCP)
```

---

## 目录

1. [快速开始](#快速开始)（5 分钟跑通本地对话）
2. [使用 TestAPI 调试（不需要飞书）](#使用-testapi-调试不需要飞书)
3. [飞书应用配置](#飞书应用配置)
4. [Langfuse 可观测（推荐）](#langfuse-可观测推荐)
5. [代码框架说明](#代码框架说明)（每个目录的职责）
6. [测试](#测试) · [端口速查](#端口速查) · [技术栈](#技术栈) · [数据本地化披露](#数据本地化披露)

---

## 快速开始

### 前置条件

- Python 3.11+
- Docker（运行沙箱容器）
- 飞书开发者账号（也可以用 TestAPI 本地调试，不强依赖飞书）
- 阿里云 DashScope API Key（Qwen3-max）

### Step 1：克隆 & 安装依赖

```bash
git clone <repo> xiaopaw-v2
cd xiaopaw-v2
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[full,dev]"
```

### Step 2：准备配置文件

```bash
cp config.yaml.example config.yaml
```

编辑 `config.yaml`，需要填写的最少配置：

```yaml
feishu:
  app_id: "cli_xxx"              # 飞书应用 App ID
  app_secret: "xxx"              # 飞书应用 App Secret

agent:
  model: "qwen3-max"             # 主 LLM（默认即可）

sandbox:
  url: "http://localhost:8030/mcp"  # 沙箱地址（与 compose 端口一致）
```

> 也支持通过环境变量覆盖：`FEISHU_APP_ID`、`FEISHU_APP_SECRET`。config.yaml 中使用 `"${ENV_VAR}"` 语法引用环境变量。

### Step 3：设置 LLM API Key

```bash
export QWEN_API_KEY="sk-xxx"   # 阿里云 DashScope API Key
```

### Step 4：启动沙箱

沙箱是 Agent 执行代码的隔离环境（Docker 容器），通过 MCP 协议通信。

```bash
docker compose -f sandbox-docker-compose.yaml up -d
```

验证沙箱启动：

```bash
curl -s http://localhost:8030/ | head -3   # 返回 JSON 即正常（/healthz 不存在属正常现象）
```

**重要：workspace 目录权限**

沙箱内的 MCP 服务以 `gem` 用户运行，需要能直接写入 `/workspace/`（即宿主机的 `./data/workspace`）。
XiaoPaw 在首次启动时会自动设置权限，但如果你手动创建或恢复了 workspace 文件，需确保权限正确：

```bash
chmod -R 777 data/workspace/
chmod 666 data/workspace/*.md
```

**如果权限不对会发生什么**：memory-save Skill 无法直接写入 workspace，沙箱会尝试绕路（`sudo cp`），被 sandbox_guard 拦截后反复重试，导致每条回复耗时数分钟。

> 沙箱将 `./xiaopaw/skills` 挂载到容器 `/mnt/skills`，将 `./data/workspace` 挂载到 `/workspace`。这两个目录必须存在。

**⚠️ 不要 `rm -rf data/workspace/`（典型坑）**

清空 workspace 时，**只删 contents、保留目录本身**：

```bash
# ✅ 正确：只清内容，目录 inode 不变
rm -rf data/workspace/* data/workspace/.[!.]*

# ❌ 错误：会破坏 docker bind mount
rm -rf data/workspace && mkdir data/workspace
```

原理：Docker bind mount 绑定的是 host 目录的 **inode**。删掉目录再 mkdir 会创建新 inode，但容器里的 `/workspace` 仍指向旧（已删除）inode —— host 写文件容器看不到，反之亦然。症状：MCP 工具调用挂死、memory-save 写入丢失、e2e 测试卡在 "MCP Connection Started" 不动。

如果已经 `rm -rf` 了，用以下命令修复：

```bash
docker compose -f sandbox-docker-compose.yaml restart

# 验证 host / container inode 一致
stat -c "host=%i" data/workspace
docker exec xiaopaw-v2-aio-sandbox-1 stat -c "container=%i" /workspace
# 两个数字必须相同，否则 mount 还是坏的
```

### Step 5：启动 pgvector（可选，记忆搜索需要）

如果只是体验基本对话，可以跳过这步。需要"三层记忆"中向量搜索功能时再启动。

```bash
# 方式 1：使用已有的 PostgreSQL（需安装 pgvector 扩展）
export MEMORY_DB_DSN="postgresql://user:pass@localhost:5432/xiaopaw"
psql "$MEMORY_DB_DSN" -f schema.sql

# 方式 2：用 Docker 快速启动
docker run -d --name pgvector \
  -e POSTGRES_USER=xiaopaw -e POSTGRES_PASSWORD=xiaopaw -e POSTGRES_DB=xiaopaw \
  -p 5432:5432 pgvector/pgvector:pg16

sleep 3
export MEMORY_DB_DSN="postgresql://xiaopaw:xiaopaw@localhost:5432/xiaopaw"
psql "$MEMORY_DB_DSN" -f schema.sql
```

然后在 `config.yaml` 中填入：

```yaml
memory:
  db_dsn: "postgresql://xiaopaw:xiaopaw@localhost:5432/xiaopaw"
```

### Step 6：启动 XiaoPaw

```bash
# 开发模式（启动 TestAPI + CaptureSender 模式，不依赖真实飞书）
export XIAOPAW_ENV=dev
python -m xiaopaw.main
```

启动成功后会看到：

```
feishu websocket listener started
test api started on 127.0.0.1:9090
metrics server started on 0.0.0.0:8090
```

### Step 7：验证

```bash
# Metrics 端点
curl http://127.0.0.1:8090/metrics

# 通过 TestAPI 发测试消息（开发模式）
curl -X POST http://127.0.0.1:9090/api/test/message \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $XIAOPAW_TESTAPI_TOKEN" \
  -d '{"routing_key": "p2p:ou_test", "text": "你好"}'
```

---

## 使用 TestAPI 调试（不需要飞书）

开发模式下（`XIAOPAW_ENV=dev`），XiaoPaw 会启动一个本地 TestAPI（默认 9090 端口），你可以直接发 HTTP 请求测试，不需要配置飞书应用。

```bash
# config.yaml 中启用：
# debug:
#   enable_test_api: true
#   test_api_port: 9090
#   test_api_token: "your-dev-token"

# 发送消息
curl -X POST http://127.0.0.1:9090/api/test/message \
  -H "Authorization: Bearer your-dev-token" \
  -d '{"routing_key": "p2p:ou_test", "text": "帮我搜索一下 Python 3.13 有什么新特性"}'

# 查看 Agent 回复
curl http://127.0.0.1:9090/api/test/replies?routing_key=p2p:ou_test
```

---

## 飞书应用配置

要在飞书中使用 XiaoPaw，需要在飞书开发者后台创建一个机器人应用。

1. **创建应用**：[飞书开放平台](https://open.feishu.cn/) → 开发者后台 → 创建企业自建应用，复制 **App ID** 和 **App Secret**。
2. **添加机器人能力**：应用能力 → 添加机器人，填写名称和描述。
3. **配置权限**：

   | 权限 | 用途 |
   |------|------|
   | `im:message:receive_v1` | 接收消息事件 |
   | `im:message` / `im:message:send_as_bot` | 发送消息 |
   | `im:resource` | 获取消息中的图片/文件 |
   | `contact:user.base:readonly` | 获取用户基本信息 |

   如果使用飞书操作技能（`feishu_ops`），还需要 `docs:doc` / `sheets:spreadsheet` / `calendar:calendar` / `bitable:app`。

4. **启用 WebSocket 模式**：事件与回调 → 选择"使用长连接接收事件"，无需公网回调地址。
5. **发布应用**：版本管理与发布 → 创建版本 → 发布（企业内部应用需管理员审批）。
6. **使用**：飞书中搜索机器人名称发私聊；或拉入群聊后 @ 机器人。

---

## Langfuse 可观测（推荐）

XiaoPaw 集成了 Langfuse 全链路追踪，可以可视化每次对话的完整调用链（LLM 调用、工具执行、Sub-Crew 流程）。

```bash
# Langfuse Cloud 或自托管实例：
export XIAOPAW_LANGFUSE_PUBLIC_KEY="pk-lf-..."
export XIAOPAW_LANGFUSE_SECRET_KEY="sk-lf-..."
export TRACE_TO_LANGFUSE=true
# export XIAOPAW_LANGFUSE_BASE_URL="https://your-langfuse.example.com"  # 默认 http://localhost:3000
```

> `XIAOPAW_` 前缀的环境变量优先于通用的 `LANGFUSE_PUBLIC_KEY`。当机器上有多个服务共享 Langfuse 时，用前缀可以避免 trace 写入错误的 project。

**自托管 Langfuse**：

```bash
git clone https://github.com/langfuse/langfuse.git
cd langfuse && docker compose up -d
# 访问 http://localhost:3000，创建 project，获取 API Key
```

---

## 代码框架说明

```
xiaopaw-v2/
├── config.yaml.example           # 配置模板
├── sandbox-docker-compose.yaml   # 沙箱 Docker Compose（端口 8030）
├── schema.sql                    # pgvector 表结构（记忆搜索）
├── DESIGN.md                     # 设计总纲
│
├── xiaopaw/                      # 主代码（业务层）
│   ├── main.py                   #   启动入口：装配 listener / runner / 监控端点
│   ├── runner.py                 #   消息队列 + Agent 调度
│   ├── agents/                   #   Main Crew + Sub-Crew 两层 Agent 编排
│   │   ├── main_crew.py          #     主 Crew（编排层）
│   │   └── skill_crew.py         #     Sub-Crew（零编排协作）
│   ├── tools/                    #   SkillLoaderTool（渐进式能力披露）
│   ├── skills/                   #   13 个技能（baidu_search/web_browse/pdf/docx/...）
│   ├── hook_framework/           #   ★ Hook 框架（事件分发 + YAML 加载）
│   │   ├── registry.py           #     EventType + HookContext + HookRegistry + GuardrailDeny
│   │   ├── crew_adapter.py       #     CrewAI 回调 → 5+2 事件映射 + pending_deny
│   │   └── loader.py             #     hooks + strategies + deps 三段式 YAML 加载
│   ├── memory/                   #   ★ 三层记忆（Bootstrap + 文件 + pgvector）
│   ├── session/                  #   会话管理（routing_key → session 状态）
│   ├── feishu/                   #   飞书 SDK（WebSocket 监听 + 消息发送）
│   ├── llm/                      #   LLM 接入（Qwen3-max via DashScope）
│   ├── config/                   #   配置校验（Pydantic）
│   └── observability/            #   指标 / 日志 / trace
│
├── shared_hooks/                 # ★ 加固层（9 个策略，1337 行，业务 0 行修改）
│   ├── hooks.yaml                #   两段式声明（72 行，本仓的"装甲接线图"）
│   ├── structured_log.py         #   JSON 事件日志（82 行）
│   ├── langfuse_trace.py         #   Langfuse 全链路（779 行，含 Trace 树五大机制）
│   ├── audit_logger.py           #   JSONL 审计日志（63 行，被 sandbox/permission deps 共享）
│   ├── sandbox_guard.py          #   路径穿越/Shell 注入/Prompt 注入消毒（107 行）
│   ├── permission_gate.py        #   工具权限三级控制 deny/warn/allow（75 行）
│   ├── cost_guard.py             #   $1 成本围栏（69 行）
│   ├── loop_detector.py          #   循环检测阈值 3（50 行）
│   └── retry_tracker.py          #   重试追踪最多 5 次（40 行）
│
├── workspace-init/               # 新用户 workspace 模板（soul/user/agent/memory）
├── tests/                        # 单元 + 集成 + E2E 测试（293 用例）
│   ├── unit/                     #   188 单测（shared_hooks 106 + hook_framework 64 + v3_fixes 18）
│   ├── integration/              #   40 集成（hook_chain / security_chain / deny_flow）
│   └── e2e/                      #   65 个 E2E 用例，15 场景 + 2 persona
│
└── docs/                         # 设计文档（18 篇 + SSOT 清单）
    ├── 01-architecture.md  02-modules.md  07-security.md  08-deployment.md
    ├── 12-hook-hardening.md      #   Hook 加固设计总纲（v3.1-rc1）
    ├── 13-test-design-hook-hardening.md
    ├── 14-e2e-test-design.md
    ├── 15-e2e-fix-structured-log-and-timing.md
    ├── langfuse-trace-fix-design.md
    └── ssot/                     #   权威清单（锁/任务/端口/feature flags/威胁）
```

### 技能列表（SkillLoaderTool 渐进式披露）

Main Crew 不知道具体技能实现，只看到技能名称和描述，需要时调用 `skill_loader` 触发 Sub-Crew 在沙箱中执行。

| 技能 | 类型 | 说明 |
|------|------|------|
| `baidu_search` | task | 百度搜索（支持时间过滤） |
| `web_browse` | task | 网页浏览、内容提取、截图 |
| `pdf` / `docx` / `pptx` / `xlsx` | task | 文档读写 |
| `feishu_ops` | task | 飞书消息/文档/表格/日历/多维表格 |
| `scheduler_mgr` | task | 定时任务管理（cron） |
| `memory-save` / `search_memory` / `memory-governance` | task | 三层记忆操作 |
| `skill-creator` | task | 动态创建新技能 |
| `history_reader` | reference | 读取完整会话历史（分页） |

---

## 测试

```bash
# 全量单元测试（188 个）
pytest tests/unit/ -v

# 加固层（shared_hooks 106 + hook_framework 64）
pytest tests/unit/shared_hooks/ tests/unit/hook_framework/ -v

# 集成测试（40 个，需 Langfuse 实例）
pytest tests/integration/ -v

# E2E（15 场景 65 用例，需 LLM + Sandbox + Langfuse）
export QWEN_API_KEY=xxx TRACE_TO_LANGFUSE=true
pytest tests/e2e/ -v

# 代码质量
ruff check .
```

测试标记：

| 标记 | 含义 |
|------|------|
| `llm_dependent` | 需要真实 LLM API |
| `sandbox` / `sandbox_required` | 需要运行中的沙箱 |
| `pgvector_required` | 需要 pgvector 数据库 |
| `security` | 安全相关测试 |
| `e2e` | 端到端测试 |

---

## 常见坑 FAQ

> 这几个坑都跟"看似成功，实际坏掉"或"5分钟挂死无报错"有关。先记住症状，遇到时直接对号入座。

### 1. memory-save 说"已记住"，但 `/new` 后召回失败

**症状**：保存阶段看到 `好的，已记住...`，但下次会话问"我是做什么的"，agent 回"不清楚"。

**根因**：`data/workspace/*.md` 文件 perms 漂移成 `644`（root 只读）。沙箱以 `gem`(UID 1000) 运行，写 `/workspace/user.md` 收 `Permission denied`，**但 LLM "创意"地 cp 出去再写到 sub-dir，最后返回成功** —— Bootstrap 只读 `/workspace/user.md`，看不到。

**自查**：
```bash
ls -la data/workspace/*.md       # 应该是 -rw-rw-rw- (666)
chmod 666 data/workspace/*.md    # 修复
```

启动 XiaoPaw 时 `xiaopaw/main.py` 会自动重置 perms，重启 XiaoPaw 通常就能自愈。memory-save SKILL.md 已加"严禁绕道"规则，新版本 LLM 遇到 `Permission denied` 会显式返回 `errcode 1003`，不再静默成功。

### 2. 测试卡在 `MCP Connection Started` 不动 5 分钟

**症状**：sub-crew 启动后只打印 `MCP Connection Started` 然后无任何输出，5 分钟后 `concurrent.futures.TimeoutError` 或测试 SocketTimeout。

**根因**（任一）：
- **Transport 不匹配**：`MCPServerSSE` 配 `/mcp`（HTTP）端点。沙箱 `/mcp` 是 Streamable HTTP，必须用 `MCPServerHTTP`。
- **URL 空串**：`MCPServerHTTP(url="")` → httpx 抛 `UnsupportedProtocol`，被 anyio TaskGroup 吞，asyncgen 关不掉，请求永远不返回。

**自查**：
```bash
# 直接测沙箱 MCP（应返回 JSON 而非 404）
curl -s -X POST http://localhost:8030/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream, application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"probe","version":"1.0"}}}'
```

`xiaopaw/agents/skill_crew.py::build_skill_crew` 已加 URL 校验，配错时直接 `ValueError` 而非挂 5min。

### 3. `rm -rf data/workspace` 之后 MCP 挂死、沙箱 host 互不可见

**症状**：删了 workspace 目录再 mkdir，跑测试发现 sandbox 写的文件 host 看不到，或反之；MCP 工具调用挂死。

**根因**：Docker bind mount 绑的是 host 目录的 inode。`rm -rf <dir> && mkdir <dir>` 创建的是新 inode，容器仍指向旧（已删）inode。

**自查 + 修复**：
```bash
stat -c "%i" data/workspace                                    # host inode
docker exec xiaopaw-v2-aio-sandbox-1 stat -c "%i" /workspace   # container inode — 必须相同
docker compose -f sandbox-docker-compose.yaml restart           # 不一致就重启重建 mount
```

**正确清空姿势**：`rm -rf data/workspace/*`（清 contents，保留目录本身）。

### 4. 怎么读 Langfuse trace 时不要被假成功骗

看 trace 树时，**根 span output ≠ 内部全部成功**。Trace 显示 "好的已记住" 不代表真的写到 user.md。

**正确读法**：
- 检查 root span `name`、`source: xiaopaw-v2`、tree 结构
- 检查每个 tool span 的 `level`（DEFAULT vs WARNING）和 `statusMessage`
- 检查最里层 file_operations 的 output JSON 里 `success` 字段
- 跨 session 的语义检查（"我能召回吗？"）才是真正的端到端断言

---

## 端口速查

| 端口 | 服务 | 说明 |
|------|------|------|
| 8030 | AIO-Sandbox MCP | 沙箱 MCP 端点（`sandbox-docker-compose.yaml`） |
| 8090 | Prometheus Metrics | 指标端点（`/metrics`） |
| 9090 | TestAPI | 开发调试 HTTP API（仅 dev 模式） |
| 5432 | PostgreSQL | pgvector 数据库（可选） |
| 3000 | Langfuse | 可观测 UI（可选，自托管时） |

完整设计文档列表：

| 文档 | 内容 |
|------|------|
| [DESIGN.md](DESIGN.md) | 设计总纲 |
| [docs/01-architecture.md](docs/01-architecture.md) | 架构总览、数据流、信任边界 |
| [docs/02-modules.md](docs/02-modules.md) | 模块职责和接口 |
| [docs/07-security.md](docs/07-security.md) | 安全威胁模型 |
| [docs/08-deployment.md](docs/08-deployment.md) | 部署指南 |
| [docs/12-hook-hardening.md](docs/12-hook-hardening.md) | Hook 框架 + 加固策略设计（v3.1-rc1） |
| [docs/13-test-design-hook-hardening.md](docs/13-test-design-hook-hardening.md) | 加固层测试设计（136 用例规格） |
| [docs/14-e2e-test-design.md](docs/14-e2e-test-design.md) | E2E 测试设计（15 场景 + 覆盖矩阵） |
| [docs/15-e2e-fix-structured-log-and-timing.md](docs/15-e2e-fix-structured-log-and-timing.md) | E2E 修复记录 |
| [docs/langfuse-trace-fix-design.md](docs/langfuse-trace-fix-design.md) | Langfuse trace 质量修复（8 个问题 P1-P8） |
| [docs/ssot/](docs/ssot/) | 权威清单（锁/任务/端口/feature flags/威胁） |

---

## 技术栈

| 组件 | 版本 | 用途 |
|------|------|------|
| Python | 3.11+ | 主语言（async/await） |
| CrewAI | >= 1.9.3 | Agent 编排 |
| lark-oapi | >= 1.3 | 飞书 SDK（WebSocket 长连接） |
| Qwen3-max | — | 主 LLM（阿里云 DashScope） |
| AIO-Sandbox | latest | MCP 执行沙盒（Docker 容器） |
| pgvector | pg16 | 记忆搜索（PostgreSQL 扩展，可选） |
| Langfuse | >= 4.0 | 可观测性（trace/generation/span，可选） |

---

## 数据本地化披露

XiaoPaw 在处理消息时，会将对话内容发送到以下外部服务：

- **阿里云 DashScope**（Qwen API）：对话内容 + embedding
- **Langfuse**（可观测）：trace 元数据（不含原始对话，可选）
- **百度千帆**（搜索技能）：搜索查询（可选）
- **飞书开放平台**：消息收发

企业部署前建议评估数据出境、商业机密保护、和数据主体权利合规要求。

---

## License

MIT
