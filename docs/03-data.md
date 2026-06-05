# 03 数据设计

> 本文是 [DESIGN.md](../DESIGN.md) §5 的详细展开。
> 读者：实现工程师 / DBA / SRE。
> 最后更新：2026-04-19（v2.1）
>
> **权威清单（SSOT）**：Feature Flags 以 [`ssot/feature-flags.md`](ssot/feature-flags.md) 为准；威胁 T1-T11 以 [`ssot/threats.md`](ssot/threats.md) 为准。
>
> **v2.1 修订重点**：
> - §7.3 DLQ 字段补 `first_failed_at` / `schedule 快照`
> - §10.4 HNSW 索引参数显式化（m=16, ef_construction=64）+ DDL vs 升级路径注释
> - §12 Feature Flag 示例对齐 ssot/feature-flags.md（新增 enable_pgvector_connection_pool；enable_webhook_signature 改名 enable_webhook_replay_cache；token_counter_mode 三值）
> - 统一 ts 字段单位为毫秒
>
> 每节按固定结构描述：**数据定位 / 格式示例 / 读写规则 / 生命周期 / v2 vs v1 差异**。

---

## 目录

1. [数据总览](#1-数据总览)
2. [飞书事件与内部消息](#2-飞书事件与内部消息)
3. [Session 索引 index.json](#3-session-索引-indexjson)
4. [JSONL 对话历史](#4-jsonl-对话历史)
5. [ctx snapshot 与 raw audit](#5-ctx-snapshot-与-raw-audit)
6. [Traces 目录](#6-traces-目录)
7. [Cron tasks 与 DLQ](#7-cron-tasks-与-dlq)
8. [Skill 定义与 SKILL.md frontmatter](#8-skill-定义与-skillmd-frontmatter)
9. [SkillLoader I/O Schema](#9-skillloader-io-schema)
10. [pgvector 记忆库 schema](#10-pgvector-记忆库-schema)
11. [Workspace 文件](#11-workspace-文件)
12. [Feature Flag 状态](#12-feature-flag-状态)
13. [Runtime 辅助缓存（v2 新增）](#13-runtime-辅助缓存v2-新增)
14. [生命周期与清理矩阵](#14-生命周期与清理矩阵)
15. [数据迁移](#15-数据迁移)

---

## 1. 数据总览

XiaoPaw v2 的数据分为**文件系统数据**（本机 `data/` 目录） / **pgvector 数据** / **workspace 文件**（沙盒可见） / **运行时内存**（进程内，不落盘）四大类。

| 类别 | 根目录 | 写者 | 读者 | 生命周期 |
|---|---|---|---|---|
| Session 索引 | `data/sessions/index.json` | `SessionManager` | `SessionManager` / `CleanupService` | 永久（按策略清） |
| 对话历史 | `data/sessions/{sid}.jsonl` | `SessionManager` | `SessionManager` / `history_reader` | 180 天 → 冷存储 |
| ctx snapshot | `data/ctx/{sid}_ctx.json` | `@before_llm_call` hook | `@before_llm_call` hook | 随 session 删 |
| raw audit | `data/ctx/{sid}_raw.jsonl` | `@before_llm_call` hook | 离线分析 / debug | 30 天 |
| Traces | `data/traces/{sid}/{ts}_{msg_id}/` | `MemoryAwareCrew` | 人工 debug | 30 天 |
| Cron tasks | `data/cron/tasks.json` | `CronService` / `scheduler_mgr` | `CronService` | 永久 |
| Cron DLQ | `data/cron/tasks.dlq.jsonl` | `CronService` | 人工处理 | 永久（人工清） |
| pgvector | `memories` 表 | `async_index_turn` | `search_memory` Skill | 180 天 → 归档 |
| Workspace files | `data/workspace/sessions/{sid}/` | Sub-Crew / 用户上传 | Sub-Crew | 随 session 删 |
| Workspace config | `data/workspace/.config/{feishu,baidu}.json` | `CleanupService` | Sub-Crew（Skills） | 启动时重写 |
| Workspace memory | `data/workspace/{soul,user,agent,memory}.md` | `memory-save` Skill | `Bootstrap` | 永久（按策略清 memory.md） |
| Feature flags | `config.yaml.feature_flags` | 运维 | `FeatureFlags registry` | 配置生命周期 |
| 运行时缓存 | 进程内 | 各模块 | 各模块 | 进程生命周期 |

**设计原则（贯穿全文）**：
- **全量写统一 write-then-rename**（原子性保证）
- **JSONL append 统一 `flush() + os.fsync()`**（持久性保证）
- **进程内并发统一 `asyncio.Lock`**（单进程架构）
- **跨进程共享文件（v2 新增）统一 `filelock.FileLock`**（Cron tasks + memory-save topic）

---

## 2. 飞书事件与内部消息

### 2.1 飞书 EventMessage（SDK `lark_oapi/api/im/v1/model/event_message.py`）

| 字段 | 类型 | 说明 |
|------|------|------|
| `message_id` | str | 消息唯一 ID，形如 `om_xxxxx` |
| `root_id` | str | 话题根消息 ID（话题群中有值，用于 `reply_in_thread`） |
| `parent_id` | str | 父消息 ID（回复链） |
| `create_time` | int | 毫秒时间戳 |
| `chat_id` | str | 会话 ID（群聊有值） |
| `thread_id` | str | 话题 ID（话题群的某话题内有值） |
| `chat_type` | str | `p2p` / `group` |
| `message_type` | str | `text` / `image` / `file` / `audio` / `post` / ... |
| `content` | str | JSON 字符串（结构按 msg_type 变化） |
| `mentions` | list | @提及列表 |

content 字段分型：

| msg_type | content JSON |
|----------|-------------|
| `text` | `{"text": "用户消息内容"}` |
| `post` | `{"title": "...", "content": [[{"tag":"text","text":"..."}]]}` |
| `image` | `{"image_key": "img_xxxxx"}` |
| `file` | `{"file_key": "file_xxxxx", "file_name": "report.pdf"}` |
| `audio` | `{"file_key": "..."}` |

### 2.2 InboundMessage（内部流转对象）

来源：v1 `xiaopaw/models.py`；v2 新增 `trace_id`。

```python
@dataclass(frozen=True)
class Attachment:
    msg_type:  str    # "image" | "file"
    file_key:  str    # 飞书 file_key / image_key
    file_name: str    # image 无 file_name 时用 "{image_key}.jpg"

@dataclass(frozen=True)
class InboundMessage:
    routing_key: str                  # "p2p:ou_xxx" | "group:oc_xxx" | "thread:oc_xxx:ot_xxx"
    content:     str                  # 纯文本（附件消息时可为空）
    msg_id:      str                  # 飞书 message_id（幂等 + trace + 下载）
    root_id:     str                  # 话题根消息 ID；非 thread 场景 = msg_id
    sender_id:   str                  # open_id（发送者）
    ts:          int                  # 创建时间（毫秒）
    is_cron:     bool = False         # True = CronService 注入的 fake 消息
    attachment:  Attachment | None = None
    trace_id:    str = ""             # v2 新增，FeishuListener 入口生成
```

**读写规则**：
- **写者**：`FeishuListener`（真实消息） / `TestAPI`（测试） / `CronService`（`is_cron=True` 的 fake 消息）
- **读者**：`SessionRouter` / `Runner` / `FeishuDownloader`
- **不可变**：`frozen=True`，任何修改需 `dataclasses.replace()`

**v2 vs v1**：
- 新增 `trace_id`：入口生成 16 位 hex，贯穿日志 / metric / trace 文件
- `InboundMessage` 本身不可变，不会被日志模块污染

---

## 3. Session 索引 index.json

### 3.1 数据定位

路径：`data/sessions/index.json`
作用：全局路由表 + 每个 `routing_key` 下所有 session 的元数据快照。

### 3.2 格式示例

```json
{
  "p2p:ou_abc123": {
    "active_session_id": "s-uuid-002",
    "sessions": [
      {
        "id": "s-uuid-001",
        "created_at": "2026-01-15T09:00:00Z",
        "verbose": false,
        "message_count": 12
      },
      {
        "id": "s-uuid-002",
        "created_at": "2026-01-20T14:00:00Z",
        "verbose": true,
        "message_count": 8
      }
    ]
  },
  "group:oc_chat456": {
    "active_session_id": "s-uuid-003",
    "sessions": [
      {
        "id": "s-uuid-003",
        "created_at": "2026-01-18T10:00:00Z",
        "verbose": false,
        "message_count": 5
      }
    ]
  }
}
```

### 3.3 字段定义

| 字段 | 类型 | 说明 |
|------|------|------|
| `active_session_id` | str | 当前活跃 session；`/new` 切换时更新 |
| `sessions[].id` | str | `s-{uuid.uuid4().hex[:12]}` |
| `sessions[].created_at` | ISO-8601 str | UTC 时间，`datetime.now(timezone.utc).isoformat()` |
| `sessions[].verbose` | bool | session 级详细模式；`/verbose` 命令切换 |
| `sessions[].message_count` | int | user+assistant 对数 ×2，避免读全量 JSONL |

### 3.4 读写规则

**并发保护**：`SessionManager._index_lock: asyncio.Lock`（单进程互斥）。

**写方式（write-then-rename 原子）**：
```python
tmp_path = self._index_path.with_suffix(".json.tmp")
tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
tmp_path.rename(self._index_path)  # POSIX rename 原子
```

**崩溃恢复**：启动时 `_ensure_dirs()` 清理残留 `.tmp` 文件（`index.json` 保持上次完整版本）。

**写者**：`SessionManager.get_or_create` / `create_new_session` / `update_verbose` / `append`（更新 `message_count`）。
**读者**：同一进程内 `SessionManager._read_index()`。
**跨进程**：v2 单节点无跨进程共享；scheduler_mgr Skill 只读不写（Skill 写 cron/tasks.json 而不是 index.json）。

### 3.5 生命周期

- **永久**：默认不删；`CleanupService` 可按"长期无活跃"策略清理（配置控制，默认关）
- **手动重置**：`TestAPI /clear` → `SessionManager.clear_all()`（清空 index + 所有 jsonl）

### 3.6 v2 vs v1 差异

- **格式完全兼容**：v1 数据可直接被 v2 加载。
- **无 schema 变更**：v2 不新增字段。
- **锁模型不变**：保持 `asyncio.Lock` + write-then-rename。
- **迁移**：直接复制 `data/sessions/index.json`（见 §15）。

---

## 4. JSONL 对话历史

### 4.1 数据定位

路径：`data/sessions/{session_id}.jsonl`
作用：每个 session 的干净对话记录（User / Assistant 成对），首行为 meta。

### 4.2 格式示例

```jsonl
{"type":"meta","session_id":"s-uuid-002","routing_key":"p2p:ou_abc123","workspace_id":"xiaopaw-hr","created_at":"2026-01-20T14:00:00Z"}
{"type":"message","role":"user","content":"帮我把这个 PDF 转成 Word","ts":1737000000000,"feishu_msg_id":"om_xxx"}
{"type":"message","role":"assistant","content":"转换完成，文件已保存到 outputs/result.docx","ts":1737000025000}
{"type":"message","role":"user","content":"每周一9点给我发周报提醒","ts":1737001000000,"feishu_msg_id":"om_yyy"}
{"type":"message","role":"assistant","content":"已创建定时任务：每周一 09:00 生成并发送周报摘要。","ts":1737001010000}
```

### 4.3 字段定义

| type | 字段 | 说明 |
|------|------|------|
| `meta`（首行） | `session_id` / `routing_key` / `workspace_id` / `created_at` | 创建 session 时写入一次 |
| `message` | `role` (`user` / `assistant`) / `content` / `ts` / `feishu_msg_id?` | **`ts` 毫秒时间戳**（Unix epoch ms，与 §6.3 `ts_start` / §7.2 `next_run_at_ms` / §10.3 `turn_ts` 单位一致）；assistant 行无 `feishu_msg_id` |

来源：v1 `SessionEntry` + `MessageEntry`。

### 4.4 读写规则

**追加模式**：`open(path, "a")` + `json.dumps(entry, ensure_ascii=False) + "\n"`，append-only，**不支持随机写**。

**并发保护**：`SessionManager._jsonl_locks[sid]: asyncio.Lock`（per-session）。
- Runner 的 per-routing_key 队列已串行化同 session 消息；
- CronService 注入的 fake 消息也走队列，但 `scheduler_mgr` Skill 写 tasks.json 与此文件无关。
- 仍保留 per-session `asyncio.Lock` 作 defense-in-depth。

**持久性**：写完后 `f.flush() + os.fsync(f.fileno())`（应对主机掉电）。

**流式倒序读（v2 加固）**：`history_reader` Skill 和 `SessionManager.load_history` 读大文件时，不全量 `read_text()` 到内存。v2 实现：
1. `asyncio.to_thread()` 把阻塞 IO 丢到线程池；
2. 逐行解析，从尾部起建立 "last N" 滑窗（`collections.deque(maxlen=N)`）；
3. 跳过 `type != "message"` 的行（过滤掉 meta）。

伪码：
```python
async def load_history(self, sid, max_turns=20):
    path = self._jsonl_path(sid)
    if not path.exists():
        return []
    return await asyncio.to_thread(self._read_tail, path, max_turns * 2)

def _read_tail(self, path, n):
    tail = collections.deque(maxlen=n)
    with open(path) as f:
        for line in f:
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec.get("type") != "message":
                continue
            tail.append(rec)
    return list(tail)
```

**读者**：`SessionManager.load_history`（Runner 注入历史） / `history_reader` Skill（LLM 分页查询）。
**写者**：`SessionManager.append`（每轮 user+assistant 成对写入）。

### 4.5 生命周期

- **默认**：180 天
- **归档**：`CleanupService` 定期扫描 mtime > 180d → 移动到 `data/sessions/archive/{yyyy-mm}/`（冷存储，gzip 压缩）
- **TestAPI /clear**：测试用，立即删除所有 jsonl

### 4.6 v2 vs v1 差异

- **格式完全兼容**：meta 首行 + message 行格式不变。
- **读取实现加固**：
  - v1 使用 `path.read_text().split("\n")`，大文件 OOM 风险；
  - v2 改为 `asyncio.to_thread + 流式倒序 deque`。
- **持久性**：v2 保持 `flush + fsync`，但在 `data/` 所在卷剩余 <10% 时 `CleanupService` 优先归档。

---

## 5. ctx snapshot 与 raw audit

### 5.1 数据定位

两个文件并存于 `data/ctx/`（路径可通过 `config.yaml.ctx_dir` 配置）：

| 文件 | 模式 | 用途 |
|------|------|------|
| `data/ctx/{sid}_ctx.json` | **overwrite** | 压缩后的跨 session 快照（加载到 LLM context） |
| `data/ctx/{sid}_raw.jsonl` | **append-only** | 完整审计日志（debug / 离线分析） |

来源：v1 `xiaopaw/memory/context_mgmt.py` 的 `save_session_ctx` / `load_session_ctx` / `append_session_raw`。

### 5.2 ctx.json 格式

结构：**OpenAI/Qwen messages 数组**（与 LLM 调用直接互通）。

```json
[
  {
    "role": "system",
    "content": "<context_summary>\n用户目标：完成 HR 周报草稿\n关键事实：文档模板 /workspace/sessions/s-xxx/uploads/template.docx\n未完成：尚未附上 Q2 数据\n</context_summary>"
  },
  {
    "role": "user",
    "content": "【当前】帮我写一份本周周报"
  },
  {
    "role": "assistant",
    "content": "已基于模板生成..."
  }
]
```

**字段**：标准 OpenAI messages（`role` / `content`，`tool_calls` 可选，`tool_call_id` 可选）。

### 5.3 raw.jsonl 格式

```jsonl
{"role":"user","content":"...","ts":"2026-01-20T14:00:05.123456+00:00"}
{"role":"assistant","tool_calls":[{"name":"skill_loader","args":{"skill_name":"pdf",...},"call_id":"c001"}],"ts":"2026-01-20T14:00:06.789012+00:00"}
{"role":"tool","name":"skill_loader","call_id":"c001","content":"{\"errcode\":0,...}","ts":"2026-01-20T14:00:25.000000+00:00"}
{"role":"assistant","content":"转换完成。","ts":"2026-01-20T14:00:26.000000+00:00"}
```

每行 = 某次 LLM 调用的某条 message + `ts`（ISO UTC）。

### 5.4 读写规则

**写者**：`@before_llm_call` hook 在每次 LLM 调用前：
1. `prune_tool_results` + `maybe_compress` 处理 `messages`（in-place）；
2. `save_session_ctx(sid, messages, ctx_dir)` 覆盖写 ctx.json；
3. `append_session_raw(sid, messages, ctx_dir)` 追加 raw.jsonl。

**读者**：
- `ctx.json`：下一轮 LLM 首次调用时，`load_session_ctx` 恢复历史到 messages 头部（只读 1 次 / session 启动）。
- `raw.jsonl`：不进 LLM，仅供人工 debug 和离线分析。

**并发**：每 session 写者唯一（同 session 串行），**不需要显式锁**。若启用多副本部署（v2 未支持），需 upgrade 到 `filelock`。

**差异要点**：

| 维度 | ctx.json | raw.jsonl |
|------|----------|-----------|
| 写模式 | overwrite（write-then-rename 可选） | append-only |
| 内容 | 压缩后 messages | 完整原始 messages |
| 大小 | ≤ context 窗口 | 随对话线性增长 |
| 用途 | 跨 session 恢复 LLM context | debug / 审计 |
| 敏感信息 | 已压缩，可能仍含 PII | 保留全文，**必须 PII mask**（v2 新增） |

### 5.5 生命周期

- `ctx.json`：随 session 删除（TestAPI /clear 或 CleanupService 归档 session 时删）。
- `raw.jsonl`：**30 天**（v2 新增明确策略，v1 无限增长）；`CleanupService` 按 mtime 扫描删除。

### 5.6 v2 vs v1 差异

- **结构完全兼容**：v1 生成的 `ctx.json` / `raw.jsonl` 可直接被 v2 读取。
- **v2 新增 30 天清理策略**：v1 无限增长，产生盘空间压力。
- **v2 新增 PII mask**：raw.jsonl 写入前经 `pii_mask.py` 过滤（手机 / 邮箱 / 身份证）。
- **v2 新增目录隔离**：`ctx_dir` 从 workspace 外移到 `data/ctx/`，避免沙盒读到。

---

## 6. Traces 目录

### 6.1 数据定位

根目录：`data/traces/{session_id}/{ts}_{msg_id}/`
每次消息处理对应一个 Trace 目录，保留**完整 LLM 执行轨迹**。

### 6.2 目录结构

```
data/traces/s-uuid-002/1737000000000_om_xxx/
├── meta.json                 # 执行摘要（单 JSON 对象）
├── main.jsonl                # 主 Agent 完整 context_messages
└── skills/
    ├── pdf.jsonl             # pdf Sub-Crew context
    └── feishu_ops.jsonl      # feishu_ops Sub-Crew context
```

目录名 `{ts_ms}_{msg_id}`：`ts_ms` 为毫秒时间戳（与 §4 JSONL `ts` / §6.3 `ts_start` 单位一致）。

### 6.3 meta.json 示例

```json
{
  "session_id":    "s-uuid-002",
  "feishu_msg_id": "om_xxx",
  "root_id":       "om_root_xxx",
  "routing_key":   "p2p:ou_abc123",
  "user_message":  "帮我把这个 PDF 转成 Word",
  "skills_called": ["pdf"],
  "duration_ms":   25340,
  "ts_start":      1737000000000,
  "ts_end":        1737000025340,
  "is_cron":       false,
  "trace_id":      "a1b2c3d4e5f60718"
}
```

**v2 新增字段**：
- `trace_id`：与 `InboundMessage.trace_id` 一致，串联日志 / metric / trace 文件。

### 6.4 main.jsonl 示例（主 Agent LLM context）

```jsonl
{"role":"system","content":"You are Orchestrator..."}
{"role":"user","content":"【历史】...\n【session目录】/workspace/sessions/s-uuid-002\n【当前】帮我把这个 PDF 转成 Word"}
{"role":"assistant","tool_calls":[{"name":"skill_loader","args":{"skill_name":"pdf","task_context":"..."},"call_id":"c001"}]}
{"role":"tool","name":"skill_loader","call_id":"c001","content":"{\"errcode\":0,\"message\":\"转换成功\",\"files\":[\"/workspace/sessions/s-uuid-002/outputs/result.docx\"]}"}
{"role":"assistant","content":"转换完成，文件已保存到 outputs/result.docx。"}
```

### 6.5 skills/\*.jsonl 格式

与 `main.jsonl` 同构（也是 messages 数组），但是 Sub-Crew 的 LLM 上下文（含 sandbox MCP tool call / result）。

### 6.6 读写规则

**写者**：`MemoryAwareCrew.run_and_index()`
- `ts_start` 毫秒时写入目录 stub；
- 每次 LLM 调用完毕，`main.jsonl` / `skills/{name}.jsonl` 追加 message；
- 执行结束 `ts_end` + `duration_ms` 一起 write-then-rename 写 `meta.json`。

**并发**：同一消息只有一个写者（Runner worker 串行），**不需要锁**。

**读者**：人工 debug / `verify_trace_coverage.py` CI 脚本（v2 新增）。

### 6.7 生命周期

- **默认 30 天**：`CleanupService` 按 mtime 删除 `{ts}_{msg_id}/` 目录
- **目录格式便于清理**：`ts` 为毫秒时间戳，直接按文件夹名排序

### 6.8 v2 vs v1 差异

- **格式完全兼容**：v1 生成的 trace 目录可被 v2 工具读取
- **v2 新增 `trace_id` 字段**到 meta.json
- **v2 新增 CI gate**：`verify_trace_coverage.py` 要求 main.jsonl 所有 user message 有对应 tool/assistant 链路（覆盖率 ≥85%）

---

## 7. Cron tasks 与 DLQ

### 7.1 数据定位

| 文件 | 作用 |
|------|------|
| `data/cron/tasks.json` | 定时任务定义（全量） |
| `data/cron/tasks.dlq.jsonl`（**v2 新增**） | 死信队列（多次重试失败的任务） |

### 7.2 tasks.json 格式

基于 v1 `xiaopaw/cron/models.py`。

```json
{
  "version": 1,
  "jobs": [
    {
      "id": "job-abc123",
      "name": "每周工作摘要",
      "enabled": true,
      "schedule": {
        "kind": "cron",
        "expr": "0 9 * * 1",
        "tz": "Asia/Shanghai",
        "at_ms": null,
        "every_ms": null
      },
      "payload": {
        "routing_key": "p2p:ou_abc123",
        "message": "请生成本周工作摘要并发给我"
      },
      "state": {
        "next_run_at_ms": 1738800000000,
        "last_run_at_ms": null,
        "last_status": null,
        "last_error": null
      },
      "created_at_ms": 1736900000000,
      "updated_at_ms": 1736900000000,
      "delete_after_run": false
    }
  ]
}
```

**schedule.kind 三种模式**：

| kind | 关键字段 | 触发行为 |
|------|---------|--------|
| `at` | `at_ms`（毫秒时间戳） | 一次性；到时触发；`delete_after_run: true` 自动删除 |
| `every` | `every_ms`（毫秒间隔） | 周期；`next_run_at_ms = last_run + every_ms` |
| `cron` | `expr` + `tz` | Cron 表达式；`croniter` 计算 `next_run_at_ms` |

**cron 模式的特殊规则**：外部（含 `scheduler_mgr` Skill）写入时，`state.next_run_at_ms` 应传 `null`；CronService 加载时按最新 `expr`/`tz` 重算（避免改了 expr 仍按旧时间触发）。

### 7.3 tasks.dlq.jsonl 格式（v2 新增）

死信队列，每行一条：

```jsonl
{"ts":"2026-01-20T14:00:00Z","first_failed_at":"2026-01-20T09:00:00Z","job_id":"job-abc123","job_name":"每周工作摘要","schedule":{"kind":"cron","expr":"0 9 * * 1","tz":"Asia/Shanghai","at_ms":null,"every_ms":null},"payload":{"routing_key":"p2p:ou_abc123","message":"..."},"error":"ConnectionError: Feishu 5xx","retry_count":5,"retry_history":[{"ts":"2026-01-20T09:00:00Z","error":"ConnectionError: Feishu 5xx"},{"ts":"2026-01-20T11:00:00Z","error":"ConnectionError: Feishu 5xx"},{"ts":"2026-01-20T14:00:00Z","error":"ConnectionError: Feishu 5xx"}],"trace_id":"a1b2c3d4e5f60718"}
```

**字段**：

| 字段 | 说明 |
|------|------|
| `ts` | ISO UTC，进入 DLQ 的时刻（最后一次失败入列时刻） |
| `first_failed_at` | ISO UTC，**首次**失败时刻（v2.1 新增，用于告警"连续失败多久"） |
| `job_id` / `job_name` | 死信任务 ID 和名称 |
| `schedule` | 死信时刻 `cron` 表达式 / `at_ms` / `every_ms` 的 schedule 快照（v2.1 新增，保留原始触发语义以便人工重放） |
| `payload` | 完整 payload（用于人工重放） |
| `error` | 最后一次失败的异常信息 |
| `retry_count` | 重试次数（达到上限才入 DLQ） |
| `retry_history` | 近 3 次失败快照 `[{ts, error}]`（v2.1 新增，可选；超过 3 条只保留最新 3 条） |
| `trace_id` | 当时的 trace_id，可定位日志 |

### 7.4 读写规则

**跨进程锁（v2 核心改动）**：

v1 使用单进程原子写（write-then-rename），但 `scheduler_mgr` Skill 在沙盒容器内通过 Python 脚本写同一文件，跨进程。v2 强制 `filelock.FileLock`：

```python
# xiaopaw/cron/storage.py（v2 新增）
from filelock import FileLock

class CronStorage:
    def __init__(self, path: Path):
        self._path = path
        self._lock = FileLock(str(path) + ".lock", timeout=10)

    def read(self) -> dict:
        with self._lock:
            return json.loads(self._path.read_text())

    def write(self, data: dict) -> None:
        with self._lock:
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
            tmp.rename(self._path)
```

- 超时 10s 报错（FeatureFlag `enable_cron_filelock=true` 时启用）；
- 锁文件 `tasks.json.lock` 需在 `.gitignore` 中忽略。

**DLQ 写入**：`CronService._on_job_failed()` 重试 5 次后 append-only 写入：
```python
with open(dlq_path, "a") as f:
    f.write(json.dumps(record, ensure_ascii=False) + "\n")
    f.flush(); os.fsync(f.fileno())
```
**注意**：DLQ 不需要 FileLock（append 单写者，CronService 进程）。

**写者**：
- `tasks.json`：`CronService`（更新 state） / `scheduler_mgr` Skill（CRUD，通过沙盒 bash 脚本）；
- `tasks.dlq.jsonl`：`CronService` 独占。

**读者**：
- `tasks.json`：`CronService`（mtime + size 热重载） / `scheduler_mgr`（查询）；
- `tasks.dlq.jsonl`：人工 / 告警脚本。

### 7.5 生命周期

- `tasks.json`：**永久**；`delete_after_run=true` 的 `at` 任务在触发后自动删除
- `tasks.dlq.jsonl`：**永久**；人工处理后 truncate 或 rotate

### 7.6 v2 vs v1 差异

| 维度 | v1 | v2 |
|------|----|----|
| tasks.json 锁 | 单进程 write-then-rename | **filelock.FileLock + timeout 10s** |
| DLQ | 无 | **新增 tasks.dlq.jsonl** |
| cron 模式 next_run | 已有"重算"规则 | 保持不变，但 FeatureFlag 控制 |
| 告警 | 无 | `xiaopaw_cron_dlq_total` metric + Prometheus 告警 |
| 数据格式 | — | 完全兼容：v1 的 tasks.json 可直接被 v2 加载 |

**迁移**：直接复制 `data/cron/tasks.json`（见 §15）。

---

## 8. Skill 定义与 SKILL.md frontmatter

### 8.1 目录结构

```
xiaopaw/skills/{skill_name}/
├── SKILL.md                # 元信息（frontmatter）+ 正文指令
├── scripts/                # （task 型）脚本化架构
│   ├── _shared_helper.py
│   ├── operation_a.py
│   └── operation_b.py
└── LICENSE.txt             # 可选
```

### 8.2 SKILL.md frontmatter（v2 加固）

```yaml
---
name: feishu_ops                          # Skill 唯一标识，必须与目录名一致
description: "飞书操作：读文档 / 发消息 / 管日历"  # XML 摘要中展示，主 Agent 选型依据
type: task                                # reference | task
version: "1.0"
allowed_tools:                            # 【v2 新增】MCP 白名单，task 型必填
  - sandbox_execute_code
  - sandbox_file_operations
---

# feishu_ops Skill

## 功能说明
...（完整执行指令，Sub-Crew 读取）
```

**字段语义**：

| 字段 | 类型 | v1 | v2 | 说明 |
|------|------|----|----|------|
| `name` | str | ✓ | ✓ | 必填；与目录名一致；SkillLoader 校验 |
| `description` | str | ✓ | ✓ | 必填；XML 摘要展示 |
| `type` | `reference` / `task` | ✓ | ✓ | 决定派发路径 |
| `version` | str | ✓ | ✓ | 可选；语义化版本 |
| `allowed_tools` | list[str] | ✗ | **✓** | **v2 新增**；MCP 工具白名单 |
| `license` | str | ✓ | ✓ | 可选；合规信息 |

### 8.3 allowed_tools 白名单（v2 新增）

生产模式下（`feature_flags.enable_mcp_whitelist=true`），Sub-Crew 仅暴露 SKILL.md 声明的 `allowed_tools`：

典型映射：

| Skill | allowed_tools |
|-------|---------------|
| `pdf` / `docx` / `pptx` / `xlsx` | `[sandbox_file_operations, sandbox_execute_code]` |
| `feishu_ops` | `[sandbox_execute_code, sandbox_file_operations]` |
| `baidu_search` | `[sandbox_execute_code]` |
| `web_browse` | `[sandbox_convert_to_markdown, browser_navigate, browser_get_markdown, browser_screenshot, browser_get_clickable_elements]` |
| `scheduler_mgr` | `[sandbox_file_operations, sandbox_execute_code]` |
| `memory-save` / `memory-governance` / `skill-creator` | `[sandbox_file_operations]` |
| `search_memory` | `[sandbox_execute_code]` |
| `history_reader` | — （reference 型，不启 Sub-Crew） |

**开发场景**：`enable_mcp_whitelist=false` 时 Sub-Crew 暴露全部 MCP 工具（与 v1 等价），方便开发者探索 `browser_*` 等工具。

### 8.4 YAML 解析安全

- 强制 `yaml.safe_load`（拒绝 `!!python/object` 等不安全 tag）
- 只解析 `---` 与下一个 `---` 之间的区块
- 路径白名单：仅读取 `xiaopaw/skills/{name}/SKILL.md`，拒绝 `..` / 绝对路径
- 异常捕获：解析失败 → `SkillLoadError`，skill 不注入，metric `xiaopaw_skill_load_error_total`

### 8.5 load_skills.yaml（注册表）

独立于 SKILL.md，列出启用哪些 skill：

```yaml
skills:
  - name: pdf
    type: task
    enabled: true
  - name: docx
    type: task
    enabled: true
  - name: feishu_ops
    type: task
    enabled: true
  - name: history_reader
    type: reference
    enabled: true
  - name: search_memory
    type: task
    enabled: true
  # ...
```

### 8.6 读写规则

**写者**：人（开发者编辑 SKILL.md） / `skill-creator` Skill（生成新 SKILL.md 到 workspace，不进主代码）。
**读者**：`SkillLoaderTool.__init__`（启动时阶段一解析 frontmatter） / `SkillLoaderTool._arun`（调用时阶段二读全文）。

**并发**：Skill 是启动时只读资源；运行时不会变更（**热加载需重启**）。

### 8.7 生命周期

- **永久**：随代码仓库管理；`skill-creator` 生成的新 SKILL.md 落在 `workspace/skills/` 下，**不**进主仓库

### 8.8 v2 vs v1 差异

- **新增 `allowed_tools` 字段**：向后兼容（缺失字段 → 用全量 MCP，由 FeatureFlag 控制）
- **YAML 解析加固**：`yaml.safe_load` 强制 + 路径白名单
- **`search_memory` 的 `routing_key`**：frontmatter 无此字段，由 SkillLoader 注入（见 §9）

---

## 9. SkillLoader I/O Schema

### 9.1 SkillLoaderInput

Pydantic 模型（来源：v1 `xiaopaw/tools/skill_loader.py`，v2 不变）：

```python
class SkillLoaderInput(BaseModel):
    skill_name: str = Field(
        description="要加载的 Skill 名称，必须严格来自工具描述 XML 列表中的 <name> 值"
    )
    task_context: str = Field(
        default="",
        description=(
            "如果是参考型 skill，此项为空。\n"
            "如果是任务型 skill，此项为子任务的完整描述（必须是字符串）。可写自然语言或 JSON。"
        ),
    )

    @field_validator("task_context", mode="before")
    @classmethod
    def task_context_to_str(cls, v):
        if v is None:
            return ""
        if isinstance(v, str):
            return v
        return json.dumps(v, ensure_ascii=False)
```

### 9.2 SkillResult

```python
class SkillResult(BaseModel):
    errcode: int                    # 0 = 成功，非 0 = 失败
    message: str                    # 人类可读摘要（主 Agent 直接用于回复）
    data:    dict = Field(default_factory=dict)   # 结构化数据
    files:   list[str] = Field(default_factory=list)  # 沙盒内绝对路径
```

### 9.3 history_reader 特殊返回（内联）

`history_reader` 不走 Sub-Crew，由 SkillLoader 直接构造结果：

```json
{
  "errcode": 0,
  "message": "成功读取第 1 页，共 35 条消息，本页 20 条",
  "data": {
    "messages": [{"role": "user", "content": "..."}],
    "total": 35,
    "page": 1,
    "page_size": 20,
    "total_pages": 2
  }
}
```

### 9.4 v2 新增校验（search_memory routing_key）

SkillLoader 在调用 `search_memory` 前强制注入 `routing_key`，拒绝 LLM 伪造跨用户查询：

```python
if skill_name == "search_memory":
    expected_rk = self._routing_key
    provided_rk = kwargs.get("routing_key")
    if provided_rk and provided_rk != expected_rk:
        return SkillResult(errcode=403, message="routing_key 校验失败，拒绝跨用户查询").model_dump()
    kwargs["routing_key"] = expected_rk  # 兜底注入
```

### 9.5 读写规则

I/O 对象**纯进程内传递**，不落盘。`SkillResult` 会被序列化进 `main.jsonl` 的 tool message（见 §6.4）。

### 9.6 v2 vs v1 差异

- **Input schema 不变**：完全兼容
- **Result schema 不变**：完全兼容
- **安全校验**：v2 在 `_run/_arun` 入口加 `routing_key` 强制校验

---

## 10. pgvector 记忆库 schema

### 10.1 数据定位

- 数据库：PostgreSQL 16 + `pgvector` 扩展
- 连接：`MEMORY_DB_DSN=postgresql://xiaopaw:xxx@pgvector:5432/xiaopaw_memory`（v2 凭证从 `.env` 注入，不入 `config.yaml`）
- Docker Compose：`pgvector-docker-compose.yaml`

### 10.2 memories 表 DDL

来源：v1 `schema.sql`，v2 加入 RLS 和 routing_key NOT NULL。

> **v2.1 注意**：以下 DDL 适用于**新建库**（`CREATE TABLE IF NOT EXISTS` 语义）。对 v1 已存在库，`routing_key NOT NULL` 约束需走 `ALTER TABLE ... SET NOT NULL` 两阶段 SOP（先回填 / 清理 NULL 行，再加约束），详见 §15.2。

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS memories (
    id              TEXT        PRIMARY KEY,           -- SHA-256 幂等 id（16 字符截断）
    session_id      TEXT        NOT NULL,
    routing_key     TEXT        NOT NULL,              -- v2 required，不允许 NULL（新建库约束；老库升级见 §15.2）

    user_message    TEXT        NOT NULL,
    assistant_reply TEXT        NOT NULL,

    summary         TEXT        NOT NULL,              -- qwen3-max 提取的一句话摘要
    tags            TEXT[]      NOT NULL DEFAULT '{}',

    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    turn_ts         BIGINT      NOT NULL,              -- 毫秒时间戳（原始对话时间）

    summary_vec     vector(1024),                      -- text-embedding-v3 维度
    message_vec     vector(1024),

    search_text     TEXT        NOT NULL DEFAULT '',
    search_tsv      TSVECTOR    GENERATED ALWAYS AS (to_tsvector('simple', search_text)) STORED
);
```

### 10.3 字段说明

| 字段 | 用途 | 检索方式 |
|------|------|---------|
| `id` | 幂等主键，SHA256(`session_id + turn_ts + user_message[:32]`)[:16] | — |
| `session_id` | 来源 session | 标量过滤 |
| `routing_key` | 用户 / 群 / 话题标识 | **多租户隔离**（WHERE 强制） |
| `user_message` / `assistant_reply` | 原始内容 | 展示 |
| `summary` | LLM 提取摘要 | 向量化（`summary_vec`） |
| `tags` | LLM 提取领域标签（工作/文件/日程/...） | GIN 索引标量过滤 |
| `turn_ts` | 对话毫秒时间戳 | 时间范围过滤 |
| `summary_vec` | 1024-d 摘要语义向量 | HNSW 近邻搜索 |
| `message_vec` | 1024-d 原文语义向量 | HNSW 近邻搜索 |
| `search_text` | `user_message + " " + tags.join(" ")` | GIN 全文索引 |
| `search_tsv` | 生成列，`to_tsvector('simple', search_text)` | BM25 近似 |

### 10.4 索引策略

> **v2.1 显式化 HNSW 参数**：`m=16, ef_construction=64` 是 pgvector 推荐的工作-内存折中（每向量索引内存 ≈ 50 bytes，单表 10M 行约 500MB）。
> **注意**：以下 DDL 用于**新建库**（对应 §10.2 DDL 语义）；对 v1 已存在库的升级路径（ALTER + 非阻塞 `CREATE INDEX CONCURRENTLY`）见 §15。

```sql
-- 向量索引（HNSW 近邻，cosine；m / ef_construction 显式）
CREATE INDEX IF NOT EXISTS memories_summary_vec_idx
    ON memories USING hnsw (summary_vec vector_cosine_ops)
    WITH (m=16, ef_construction=64);

CREATE INDEX IF NOT EXISTS memories_message_vec_idx
    ON memories USING hnsw (message_vec vector_cosine_ops)
    WITH (m=16, ef_construction=64);

-- 查询时可 SET hnsw.ef_search=80（默认 40）以提高召回
-- 示例：
--   SET LOCAL hnsw.ef_search = 80;
--   SELECT ... FROM memories ORDER BY summary_vec <=> $1 LIMIT 20;

-- 全文索引（BM25 近似）
CREATE INDEX IF NOT EXISTS memories_search_tsv_idx
    ON memories USING gin (search_tsv);

-- 标量索引
CREATE INDEX IF NOT EXISTS memories_routing_key_idx ON memories (routing_key);
CREATE INDEX IF NOT EXISTS memories_created_at_idx  ON memories (created_at DESC);
CREATE INDEX IF NOT EXISTS memories_tags_idx        ON memories USING gin (tags);
```

### 10.5 RLS（Row-Level Security，v2 可选）

启用后，数据库层面强制 session 级隔离：

```sql
ALTER TABLE memories ENABLE ROW LEVEL SECURITY;

CREATE POLICY memories_isolation ON memories
    USING (routing_key = current_setting('xiaopaw.current_routing_key', true));
```

`search_memory` Skill 执行查询前设置会话变量：
```sql
SET LOCAL xiaopaw.current_routing_key = 'p2p:ou_abc123';
```

**FeatureFlag 控制**：`enable_pgvector_rls=false` 时开发模式 / 单用户可关闭（v2 默认关，生产单节点配合 app 层防护即可；多租户部署时必须开）。

### 10.6 幂等写入

```sql
INSERT INTO memories (
    id, session_id, routing_key,
    user_message, assistant_reply,
    summary, tags,
    turn_ts,
    summary_vec, message_vec,
    search_text
) VALUES (
    %(id)s, %(session_id)s, %(routing_key)s,
    %(user_message)s, %(assistant_reply)s,
    %(summary)s, %(tags)s,
    %(turn_ts)s,
    %(summary_vec)s::vector, %(message_vec)s::vector,
    %(search_text)s
)
ON CONFLICT (id) DO NOTHING;
```

- 同一轮对话重入（比如 `async_index_turn` 重试），`id` 相同 → 跳过。
- `search_tsv` 由生成列自动填充。

### 10.7 混合检索（search_memory Skill）

查询模板：

```sql
WITH vec_hits AS (
    SELECT id, 1 - (summary_vec <=> %(q_vec)s::vector) AS vec_score
    FROM memories
    WHERE routing_key = %(rk)s
      AND (%(since_ts)s IS NULL OR turn_ts >= %(since_ts)s)
      AND (%(tags)s::text[] = '{}' OR tags && %(tags)s::text[])
    ORDER BY summary_vec <=> %(q_vec)s::vector
    LIMIT 20
), ft_hits AS (
    SELECT id, ts_rank(search_tsv, plainto_tsquery('simple', %(q)s)) AS ft_score
    FROM memories
    WHERE routing_key = %(rk)s
      AND search_tsv @@ plainto_tsquery('simple', %(q)s)
    LIMIT 20
)
SELECT m.*,
       COALESCE(v.vec_score, 0) * 0.7 + COALESCE(f.ft_score, 0) * 0.3 AS score
FROM memories m
LEFT JOIN vec_hits v ON v.id = m.id
LEFT JOIN ft_hits  f ON f.id = m.id
WHERE m.routing_key = %(rk)s
  AND (v.id IS NOT NULL OR f.id IS NOT NULL)
ORDER BY score DESC
LIMIT %(topK)s;
```

### 10.8 读写规则

**写者**：`async_index_turn`（每轮对话结束后 Runner `create_task` 触发），通过 `run_in_executor` 在线程池执行同步 `psycopg2` 写入。
**读者**：`search_memory` Skill（沙盒内 `scripts/search.py`，psycopg2 直连）。
**并发**：pgvector 扩展支持并发写；`ON CONFLICT DO NOTHING` 保证幂等。
**连接池**：v1 每次 `psycopg2.connect` + `close`；v2 可选使用 `psycopg_pool`（FeatureFlag 控制）。

### 10.9 生命周期

- **默认保留 180 天**；`CleanupService` 定期：
```sql
DELETE FROM memories WHERE turn_ts < (EXTRACT(EPOCH FROM NOW()) - 180*86400)*1000;
```
- **归档前导出**：`COPY memories TO '/archive/memories_yyyy-mm.csv'`（可选）

### 10.10 v2 vs v1 差异

| 维度 | v1 | v2 |
|------|----|----|
| DDL | 完整 | **兼容 + `routing_key NOT NULL` 强制** |
| 索引 | 同样的 HNSW/GIN/btree | 不变 |
| 连接 | 每次 connect | 可选连接池 |
| RLS | 无 | **可选启用** |
| 幂等 | ON CONFLICT DO NOTHING | 不变 |
| routing_key 校验 | Skill 层可选 | **三层强制**（script required + SKILL required + SkillLoader 拒绝覆盖） |

**迁移**：`CREATE TABLE IF NOT EXISTS` 幂等；v1 数据直接可用（见 §15）。

---

## 11. Workspace 文件

Workspace 是挂载进 AIO-Sandbox 的目录，是 L20 文件记忆层的物理载体。

### 11.1 目录结构

```
data/workspace/
├── .config/                                # 凭证目录（mode 0700）
│   ├── feishu.json                        # mode 0600
│   └── baidu.json                         # mode 0600
├── soul.md                                 # Agent 人格定义（永久）
├── user.md                                 # 用户偏好（永久，memory-save 写入）
├── agent.md                                # onboarding SOP（完成后自毁）
├── memory.md                               # 语义记忆（按策略清理，200 行内）
└── sessions/
    └── {session_id}/
        ├── uploads/                        # 用户上传文件（FeishuDownloader 写入）
        ├── outputs/                        # Skill 产出
        └── tmp/                            # Sub-Crew 临时工作区
```

### 11.2 沙盒内可见路径（docker-compose 挂载）

```yaml
volumes:
  - ./data/workspace:/workspace
```

沙盒内路径：
- `/workspace/.config/feishu.json`
- `/workspace/.config/baidu.json`
- `/workspace/soul.md` / `user.md` / `agent.md` / `memory.md`
- `/workspace/sessions/{sid}/uploads/`
- `/workspace/sessions/{sid}/outputs/`
- `/workspace/sessions/{sid}/tmp/`

### 11.3 凭证文件（v2 加固）

`feishu.json` 示例：

```json
{
  "APP_ID":              "cli_xxx",
  "APP_SECRET":          "xxx",
  "ENCRYPT_KEY":         "xxx",
  "VERIFICATION_TOKEN":  "xxx"
}
```

`baidu.json`：

```json
{
  "BAIDU_API_KEY": "bce-v3/xxxxx"
}
```

**读写规则**：
- **写者**：`CleanupService.write_feishu_credentials()` / `write_baidu_credentials()`（**启动时**从 env 注入；umask 先设 0077，写入后 `os.chmod(0o600)` 二次确认）；
- **读者**：沙盒内 Skill 脚本（`_feishu_auth.py` / `search.py`）
- **密钥空时**：`baidu.json` 静默跳过（Skill 不可用但主流程不受影响）
- **密钥轮换**：运维更新 `.env` → SIGHUP / 重启服务 → `CleanupService` 重写凭证

### 11.4 Memory 文件（L20 文件记忆层）

| 文件 | 写者 | 读者 | 生命周期 | 大小限制 |
|------|------|------|---------|---------|
| `soul.md` | 开发者（workspace-init 模板）/ 运维 | `Bootstrap` | 永久 | — |
| `user.md` | `memory-save` Skill | `Bootstrap` | 永久 | 建议 ≤1000 行 |
| `agent.md` | `workspace-init` / `memory-save` | `Bootstrap` | onboarding 完成后自删 | — |
| `memory.md` | `memory-save` Skill | `Bootstrap`（**截断到 250 行告警**） | 按策略清理 | 250 行（硬上限） |

**memory-save 并发保护（v2 新增）**：

```python
from filelock import FileLock

def save_memory(topic: str, content: str, workspace_dir: Path):
    topic_path = workspace_dir / f"{topic}.md"
    lock = FileLock(str(topic_path) + ".lock", timeout=10)
    with lock:
        # append + 长度检查 + BLOCKED_PATTERNS 过滤
        ...
```

- `memory-save` Skill 可能被 Sub-Crew（沙盒进程）调用，必须用**跨进程文件锁**
- 超时 10s 报错，metric `xiaopaw_memory_save_timeout_total`
- FeatureFlag `enable_memory_save_filelock=true` 控制

**BLOCKED_PATTERNS 过滤（T2 防御）**：
- 屏蔽"忽略以上指令"、"系统提示"等投毒关键字
- 内容长度限制：≤2000 字符/条

### 11.5 Session 工作空间

**隔离规则**：
- docker-compose mount 整个 `workspace/`，但 SkillLoader 向 LLM 暴露的 session 目录路径精确到 `/workspace/sessions/{sid}/`
- `session_id` **不进入** LLM context（不在 tasks.yaml / akickoff inputs）
- Sub-Crew 在 backstory 里被约束"只读写 /workspace/sessions/{sid}/ 和 /workspace/.config/"
- **v2 加固**（T5 防御）：`skill-creator` 等写文件类 Skill 内部做 `Path.resolve()` 越界校验

### 11.6 生命周期

- `.config/`：**启动时重写**
- `soul.md` / `user.md` / `memory.md`：**永久**（memory.md 按 250 行告警）
- `agent.md`：onboarding 完成 **自毁**
- `sessions/{sid}/`：随 session 删除

### 11.7 v2 vs v1 差异

- **凭证权限**：v2 `chmod 0600` 强制（v1 仅写入，无 chmod）
- **memory-save 锁**：v2 引入 `filelock.FileLock` per topic（v1 无锁）
- **BLOCKED_PATTERNS**：v2 新增（v1 无过滤）
- **路径遍历防护**：v2 `Path.resolve()` 越界校验（v1 仅字符串拼接）

---

## 12. Feature Flag 状态

### 12.1 数据定位

路径：`config.yaml`（运维维护，不入库）
注册表：`xiaopaw/config/flags.py`（代码定义每个 flag 的 key、默认值、说明）

### 12.2 格式示例

> **SSOT 权威清单**：本节示例以 [`ssot/feature-flags.md`](ssot/feature-flags.md) 为准（v2.1 共 12 个 flag，F1-F12）。
> **v2.1 关键改名**：`enable_webhook_signature` → `enable_webhook_replay_cache`（WS 模式飞书 SDK 不提供验签 API，v2.1 改为应用层 ReplayCache 防御 T3）。

```yaml
feature_flags:
  # Tokenizer（F1，三值）
  token_counter_mode: "qwen_official"       # qwen_official / hf_qwen / rough

  # 并发与容错（F2-F5）
  enable_skill_timeout:            true     # F2：Skill 超时熔断
  enable_cron_filelock:            true     # F3：跨进程 cron 锁
  enable_memory_save_filelock:     true     # F4：memory-save 并发锁
  enable_feishu_rate_limit_aware:  true     # F5：真实 429 识别

  # 可观测（F6）
  enable_trace_id:                 true     # F6：trace 贯穿

  # 安全（F7-F10）
  enable_mcp_whitelist:            true     # F7：T1 MCP 白名单（开发环境可关）
  enable_memory_save_filter:       true     # F8：T2/T8 memory-save 投毒过滤
  enable_webhook_replay_cache:     true     # F9：T3 飞书 webhook 重放（v2.1：原 enable_webhook_signature 改名）
  enable_inbound_rate_limit:       true     # F10：T7 入站速率限制

  # pgvector（F11-F12）
  enable_pgvector_rls:             false    # F11：T11 多租户部署时开启
  enable_pgvector_connection_pool: true     # F12：v2.1 新增（性能）
```

### 12.3 读写规则

- **写者**：运维修改 `config.yaml` → SIGHUP 触发 `reload()` → `FeatureFlags.update()` 生效
- **读者**：各模块在执行分支前 `if flags.enable_xxx: ...`
- **Metric**：每个 flag 对应 `xiaopaw_feature_flag{name, enabled}`

### 12.4 生命周期

- **配置生命周期**：随 `config.yaml` 管理
- **热重载**：SIGHUP 触发；部分 flag 需要重启才能生效（Skill `allowed_tools` 因为 `SkillLoaderTool.__init__` 时固化）

### 12.5 v2 vs v1 差异

- **v1 无 feature_flags 配置节**
- **v2 新增**：作为"开发模式 ↔ 生产加固"的开关；每个 flag 对应 [ssot/feature-flags.md](ssot/feature-flags.md) 中的 F1-F12
- **v2.1 对照（改名）**：`enable_webhook_signature` → `enable_webhook_replay_cache`（v1→v2.0→v2.1 改名对照；从 v2.0 升级时需在 config.yaml 中改名）；`token_counter_mode` 二值 → 三值（新增 `hf_qwen`）；新增 `enable_pgvector_connection_pool`

---

## 13. Runtime 辅助缓存（v2 新增）

进程内缓存，**不落盘**，进程生命周期。

### 13.1 ReplayCache（飞书 webhook 去重）

位置：`xiaopaw/feishu/listener.py`（v2 新增）

```python
class ReplayCache:
    def __init__(self, maxsize: int = 10000, ttl_sec: int = 300):
        self._cache: dict[str, float] = {}  # event_id → ts
        self._ttl = ttl_sec
        self._maxsize = maxsize
        self._lock = asyncio.Lock()  # v2.1：对齐 ssot/locks.md#L5

    async def seen(self, event_id: str) -> bool:
        now = time.monotonic()
        async with self._lock:
            if event_id in self._cache:
                if now - self._cache[event_id] < self._ttl:
                    return True
            self._cache[event_id] = now
            self._evict_if_needed()
            return False
```

**参数**：
- `maxsize = 10000`：LRU 上限
- `ttl_sec = 300`：5 分钟窗口（飞书官方重放窗口）
- `_lock`（v2.1 新增，[ssot/locks.md#L5](ssot/locks.md)）：`asyncio.Lock` 保护 LRU dict 的 check→put→evict 三步，避免 FeishuListener 并发回调时 dict 改动竞态 / 容量越界

**读写**：FeishuListener 入口调用 `seen(event_id)`，返回 True 则静默丢弃。

### 13.2 TokenCounter 缓存

位置：`xiaopaw/memory/token_counter.py`（v2 新增）

```python
_tokenizer = None  # 模块级惰性缓存

def _get_tokenizer():
    global _tokenizer
    if _tokenizer is None:
        try:
            from dashscope import get_tokenizer
            _tokenizer = get_tokenizer("qwen-max")
        except Exception:
            try:
                from transformers import AutoTokenizer
                _tokenizer = AutoTokenizer.from_pretrained("Qwen/Qwen2-7B-Instruct")
            except Exception:
                _tokenizer = "rough"
    return _tokenizer
```

- **首次调用**：10-30s 加载 tokenizer（阻塞）
- **后续**：直接命中 `_tokenizer`
- **降级**：3 级 fallback（dashscope → HuggingFace → rough `len//2`）

### 13.3 SkillLoader SKILL.md 缓存

位置：`xiaopaw/tools/skill_loader.py`

- 阶段一 `__init__` 时解析 `frontmatter` → `self._manifests: dict[str, dict]`
- 阶段二调用时 `_get_skill_instructions()` 读全文 → `self._instructions_cache[name]`
- **失效**：进程生命周期，代码热更新需重启

### 13.4 LRU 会话锁（v2 替换 v1 无界 dict）

`SessionManager._jsonl_locks` 在 v1 是无界 `dict[str, asyncio.Lock]`，长跑可能 OOM。v2 改为 LRU：

```python
from cachetools import LRUCache

class SessionManager:
    def __init__(self, data_dir: Path, max_sessions: int = 1000):
        self._jsonl_locks: LRUCache[str, asyncio.Lock] = LRUCache(maxsize=max_sessions)
```

- 超出 1000 个 session → 最久未使用的锁被淘汰
- 淘汰时锁若被持有，闭包强引用保证其不被 GC（见 [05-concurrency.md §3](05-concurrency.md)）

### 13.5 生命周期

- **进程生命周期**：启动 → 清空 → 累积 → 进程退出
- **无需手动清理**

---

## 14. 生命周期与清理矩阵

### 14.1 清理策略总览

| 数据 | 保留期 | 清理者 | 清理方式 |
|------|-------|--------|---------|
| `index.json` | 永久 | — | 无自动清理 |
| `{sid}.jsonl` | 180 天 | `CleanupService` | 按 mtime 归档到 `archive/{yyyy-mm}/`，gzip |
| `{sid}_ctx.json` | 随 session | `CleanupService` | session 归档时一并删 |
| `{sid}_raw.jsonl` | 30 天 | `CleanupService` | 按 mtime 删除 |
| `traces/{sid}/{ts}_{msg_id}/` | 30 天 | `CleanupService` | 按目录 mtime 删除 |
| `cron/tasks.json` | 永久 | — | 无自动清理（`delete_after_run` 触发后删单条） |
| `cron/tasks.dlq.jsonl` | 永久 | 人工 | 人工处理后 truncate/rotate |
| pgvector `memories` | 180 天 | `CleanupService` | SQL DELETE WHERE turn_ts < ... |
| `workspace/sessions/{sid}/` | 随 session | `CleanupService` | `shutil.rmtree` |
| `workspace/.config/*` | 启动重写 | `CleanupService` | 启动时 `write_*_credentials` |
| `workspace/memory.md` | 永久 | `memory-governance` Skill | 人工触发 + 250 行告警 |
| `ReplayCache` | 5 分钟 | 进程内 LRU | 自动淘汰 |
| 日志 `data/logs/xiaopaw.log` | 14 天 | `logging.TimedRotatingFileHandler` | 按日切分 |

### 14.2 CleanupService 触发时机

- **启动时**（一次性）：
  - `sweep()`：清 `data/sessions/*.tmp` 残留
  - `ensure_workspace_dirs()`：创建 `.config` / `sessions/` / 初始化 memory 四件套
  - `write_feishu_credentials()` / `write_baidu_credentials()`：写 `.config`（chmod 0600）
- **定时**（daily cron）：
  - 扫描 session jsonl > 180 天 → 归档
  - 扫描 raw.jsonl / traces > 30 天 → 删除
  - pgvector DELETE > 180 天
  - logs rotate（由 logging 框架处理）

---

## 15. 数据迁移

详细步骤见 [11-migration-v1-to-v2.md](11-migration-v1-to-v2.md)。此处仅汇总兼容性矩阵。

### 15.1 格式兼容矩阵

| 数据 | v1 → v2 | 迁移动作 |
|------|---------|---------|
| `data/sessions/index.json` | ✓ 完全兼容 | 直接复制 |
| `data/sessions/{sid}.jsonl` | ✓ 完全兼容 | 直接复制 |
| `data/ctx/{sid}_ctx.json` | ✓ 完全兼容 | 直接复制 |
| `data/ctx/{sid}_raw.jsonl` | ✓ 完全兼容 | 直接复制；30 天以外的可选清理 |
| `data/traces/...` | ✓ 完全兼容 | 直接复制；30 天以外的可选清理 |
| `data/cron/tasks.json` | ✓ 完全兼容 | 直接复制；首次启动会补齐 `.lock` 文件 |
| `data/cron/tasks.dlq.jsonl` | v1 无 | 无需迁移（v2 空文件起步） |
| pgvector `memories` 表 | ✓ 完全兼容 | `CREATE TABLE IF NOT EXISTS` 幂等；**新增 `routing_key NOT NULL` 约束需 `ALTER TABLE memories ALTER COLUMN routing_key SET NOT NULL`**（确认 v1 无 NULL 行） |
| `data/workspace/sessions/` | ✓ 完全兼容 | 直接复制 |
| `data/workspace/.config/` | ✓ 兼容但**必须重写** | v2 启动时从 env 重写（v1 可能是 dummy 值） |
| `data/workspace/{soul,user,agent,memory}.md` | ✓ 完全兼容 | 直接复制 |
| `SKILL.md` frontmatter | ✓ 兼容但**建议补齐 `allowed_tools`** | v2 缺失 → 由 FeatureFlag 控制是否降级为全量 MCP |
| `config.yaml` | ✗ 需改造 | 新增 `feature_flags:` 节；凭证移到 `.env` |

### 15.2 ALTER 脚本示例

> **与 §10.2 DDL 的关系**：§10.2 是新建库语义（`CREATE TABLE IF NOT EXISTS` + 列级 `NOT NULL`）；本节为老库（v1）升级路径，两阶段 SOP——**先回填 / 清理 NULL，再加约束**，避免对生产表 `ALTER` 时因 NULL 行失败回滚。

```sql
-- 阶段一：盘点 NULL 行
SELECT COUNT(*) FROM memories WHERE routing_key IS NULL;
-- 若 >0，先按 session_id 回填（或人工清理）：
-- UPDATE memories m SET routing_key = s.routing_key
-- FROM (SELECT DISTINCT session_id, routing_key FROM ...) s
-- WHERE m.session_id = s.session_id AND m.routing_key IS NULL;

-- 阶段二：应用约束（此时应为 0 NULL）
ALTER TABLE memories ALTER COLUMN routing_key SET NOT NULL;

-- （可选）启用 RLS
ALTER TABLE memories ENABLE ROW LEVEL SECURITY;
CREATE POLICY memories_isolation ON memories
    USING (routing_key = current_setting('xiaopaw.current_routing_key', true));

-- （可选）v2.1 HNSW 索引非阻塞重建（老库若需要显式 m / ef_construction 参数）：
-- CREATE INDEX CONCURRENTLY memories_summary_vec_idx_v21
--     ON memories USING hnsw (summary_vec vector_cosine_ops)
--     WITH (m=16, ef_construction=64);
-- 完成后 DROP 旧索引 + RENAME 新索引。
```

### 15.3 零停机蓝绿切换

1. 在同一台机器启动 v2（不同端口），共享 `data/` 目录（只读挂载验证）
2. v2 先运行 72h canary（独立 pgvector 副本或同库不同 routing_key 测试）
3. 飞书 WebSocket 切到 v2（lark-oapi 客户端重连不需要停机）
4. v1 下线，`data/` 正式交给 v2

---

## 下一步阅读

- **模块如何使用这些数据** → [02-modules.md](02-modules.md)
- **接口数据 envelope** → [04-api.md](04-api.md)
- **并发锁的数据面 rationale** → [05-concurrency.md](05-concurrency.md)
- **trace_id / metric / PII 的数据打点** → [06-observability.md](06-observability.md)
- **数据面威胁模型（T1–T7）** → [07-security.md](07-security.md)
- **配置文件完整字段** → [09-config.md](09-config.md)
- **v1 → v2 迁移脚本** → [11-migration-v1-to-v2.md](11-migration-v1-to-v2.md)
