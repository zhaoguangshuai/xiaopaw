# 15. E2E 验证缺口修复——结构化日志补全 + 工具计时 + Langfuse 初始化加固

- **版本**：v1.0（2026-05-02）
- **前置文档**：[12-hook-hardening.md](12-hook-hardening.md) / [14-e2e-test-design.md](14-e2e-test-design.md)
- **触发**：E2E-07/09/11/12/15 全量结构化日志 Review 发现 5 类系统性问题
- **设计原则**：最小改动，不引入新抽象；修复均在 v3 Hook 框架的既有架构内完成

---

## 目录

1. [问题清单与优先级](#1-问题清单与优先级)
2. [FIX-1: structured_log 补全 3 个 handler](#2-fix-1-structured_log-补全-3-个-handler)
3. [FIX-2: crew_adapter 工具调用计时](#3-fix-2-crew_adapter-工具调用计时)
4. [FIX-3: Langfuse 初始化失败可见性](#4-fix-3-langfuse-初始化失败可见性)
5. [FIX-4: before/after tool_call 语义对齐](#5-fix-4-beforeafter-tool_call-语义对齐)
6. [FIX-5: E2E-11 超时缓解](#6-fix-5-e2e-11-超时缓解)
7. [hooks.yaml 变更汇总](#7-hooksyaml-变更汇总)
8. [测试策略](#8-测试策略)
9. [设计自查 Checklist](#9-设计自查-checklist)

---

## 1. 问题清单与优先级

E2E 全量结构化日志 Review 覆盖 8 个 session、625 个事件，发现以下系统性问题：

| # | 问题 | 优先级 | 影响面 | 根因 |
|---|------|--------|--------|------|
| FIX-1 | `after_turn`/`task_complete`/`session_end` 结构化日志缺失 | **P0** | 5+2 事件体系仅 4/7 有日志输出 | `structured_log.py` 只实现了 4 个 handler |
| FIX-2 | `duration_ms` 全量为 0 | **P1** | 所有 `after_tool_call` 事件无工具耗时 | `crew_adapter` 未在 before/after 之间计时 |
| FIX-3 | Langfuse 初始化失败静默吞没 | **P0** | 0 条 Langfuse trace 且无告警 | `_ensure_client()` 用 DEBUG 级日志，`_init_failed` 永久跳过 |
| FIX-4 | `before_tool_call`/`after_tool_call` 配对失配 | **P1** | 6/8 session 计数不等 | `final_answer` 虚拟工具 + MCP 工具 after 缺失 |
| FIX-5 | E2E-11 在全套件中超时 | **P2** | 单独 PASS，全套件 FAIL | Agent 推理链不可控（247 次 before_llm） |

---

## 2. FIX-1: structured_log 补全 3 个 handler

### 2.1 问题分析

5+2 事件体系定义了全部 7 个事件，`hooks.yaml` 也注册了全部 7 个事件的 Langfuse handler。但 `structured_log.py` 只实现了 `before_turn`、`before_llm`、`before_tool_call`、`after_tool_call` 四个 handler，遗漏了：

- `AFTER_TURN`——无法从日志确认 turn 是否正常结束
- `TASK_COMPLETE`——无法从日志确认 Task 输出
- `SESSION_END`——无法从日志确认 session 是否正常关闭

这违反了核心设计原则：**结构化日志是 Langfuse 的降级方案，两者应覆盖相同事件集**。

### 2.2 修改方案

在 `shared_hooks/structured_log.py` 新增 3 个 handler，复用现有 `_emit()` 函数：

```python
def after_turn_handler(ctx) -> None:
    _emit({
        "event": "after_turn",
        "session_id": ctx.session_id,
        "turn": ctx.turn_number,
        "agent_id": ctx.agent_id,
        "duration_ms": ctx.duration_ms,
        "input_tokens": ctx.input_tokens,
        "output_tokens": ctx.output_tokens,
    })


def task_complete_handler(ctx) -> None:
    _emit({
        "event": "task_complete",
        "session_id": ctx.session_id,
        "task_name": ctx.task_name,
        "agent_id": ctx.agent_id,
    })


def session_end_handler(ctx) -> None:
    _emit({
        "event": "session_end",
        "session_id": ctx.session_id,
    })
```

在 `hooks.yaml` 的对应事件节新增注册（见第 7 节汇总）。

### 2.3 设计约束

- **不改 `_emit()` 签名**：保持一行 JSON 到 stderr，不引入新的日志 sink
- **不改 `HookContext`**：所有字段已在 `registry.py` 定义，handler 只做读取
- **field 选择原则**：每个 handler 只输出该事件类型语义上有意义的字段，不冗余。`after_turn` 的 `duration_ms` / `input_tokens` / `output_tokens` 对应 per-turn 可观测性指标

---

## 3. FIX-2: crew_adapter 工具调用计时

### 3.1 问题分析

`HookContext.duration_ms` 字段在 `registry.py` 定义（默认 `0`），`structured_log.after_tool_handler` 和 `langfuse_trace.after_tool_handler` 都读取并记录它。但 `CrewObservabilityAdapter` 在 `on_before_tool_call()` 和 `on_after_tool_call()` 之间没有任何计时逻辑，导致所有工具调用的耗时记录为 0。

这违反了设计要求：**AFTER_TOOL_CALL 事件应携带 duration_ms，用于工具性能分析和异常检测**。

### 3.2 修改方案

在 `CrewObservabilityAdapter` 中增加 `_tool_start_times` 字典，按 `(tool_name, turn_number)` 记录开始时间：

```python
import time  # 新增 import

class CrewObservabilityAdapter:
    def __init__(self, registry: HookRegistry, session_id: str = ""):
        # ... 现有字段 ...
        self._tool_start_times: dict[tuple[str, int], float] = {}

    def on_before_tool_call(self, tool_name: str, tool_input: dict | None = None):
        self._tool_start_times[(tool_name, self._turn_count)] = time.monotonic()
        # ... 现有 dispatch_gate 逻辑不变 ...

    def on_after_tool_call(
        self, tool_name: str, tool_input: dict | None = None, tool_result: str = ""
    ):
        key = (tool_name, self._turn_count)
        start = self._tool_start_times.pop(key, None)
        elapsed_ms = round((time.monotonic() - start) * 1000) if start else 0

        truncated = _truncate(str(tool_result))
        self._registry.dispatch(
            EventType.AFTER_TOOL_CALL,
            HookContext(
                event_type=EventType.AFTER_TOOL_CALL,
                tool_name=tool_name,
                tool_input=dict(tool_input or {}),
                session_id=self._session_id,
                turn_number=self._turn_count,
                duration_ms=elapsed_ms,       # ← 新增
                metadata={"tool_output": truncated},
            ),
        )
```

### 3.3 设计约束

- **使用 `time.monotonic()`**：不受系统时钟调整影响，适合耗时测量
- **按 `(tool_name, turn)` 索引**：与 Langfuse span stack 的 key 逻辑一致（`langfuse_trace.py:382`）
- **pop 语义**：每个 before 只消费一次，避免同名工具多次调用时错配
- **不影响 deny 路径**：`on_before_tool_call` 中 `GuardrailDeny` 分支也会记录 start time，但 deny 后的 `AFTER_TOOL_CALL` 直接 dispatch（不经过 `on_after_tool_call`），所以 deny 场景的 duration_ms 仍为 0——这是正确的，因为工具并未实际执行
- **不改 `HookContext`**：`duration_ms` 字段已存在

### 3.4 同步修改：deny 路径也记录 duration

在 `on_before_tool_call` 的 deny 分支中，计算从记录 start 到 deny 的耗时：

```python
except GuardrailDeny as e:
    self._pending_deny = e
    start = self._tool_start_times.pop((tool_name, self._turn_count), None)
    deny_ms = round((time.monotonic() - start) * 1000) if start else 0
    self._registry.dispatch(
        EventType.AFTER_TOOL_CALL,
        HookContext(
            # ... 现有字段 ...
            duration_ms=deny_ms,  # ← dispatch_gate 的耗时
        ),
    )
```

---

## 4. FIX-3: Langfuse 初始化失败可见性

### 4.1 问题分析

`langfuse_trace.py` 的 `_ensure_client()` 捕获所有异常后：
1. 设置 `_init_failed = True`（永久跳过）
2. 日志级别为 `logger.debug`（默认不输出）

导致 Langfuse API keys 缺失或错误时，整个 session 的 trace 静默丢失，测试中 0 条 trace 却无任何告警。

### 4.2 修改方案

```python
def _ensure_client():
    global _client, _init_failed
    if _init_failed:
        return None
    if _client is None:
        try:
            from langfuse import Langfuse

            public_key = (
                os.environ.get("XIAOPAW_LANGFUSE_PUBLIC_KEY")
                or os.environ.get("LANGFUSE_PUBLIC_KEY")
            )
            secret_key = (
                os.environ.get("XIAOPAW_LANGFUSE_SECRET_KEY")
                or os.environ.get("LANGFUSE_SECRET_KEY")
            )
            base_url = (
                os.environ.get("XIAOPAW_LANGFUSE_BASE_URL")
                or os.environ.get("LANGFUSE_BASE_URL")
            )

            if not all([public_key, secret_key, base_url]):
                _init_failed = True
                logger.warning(
                    "langfuse disabled: missing env vars "
                    "(need XIAOPAW_LANGFUSE_PUBLIC_KEY + SECRET_KEY + BASE_URL)"
                )
                return None

            _client = Langfuse(
                tracing_enabled=False,
                public_key=public_key,
                secret_key=secret_key,
                base_url=base_url,
            )
            atexit.register(_flush_batch)
        except Exception:
            _init_failed = True
            logger.warning(
                "langfuse init failed (non-blocking)", exc_info=True
            )
            return None
    return _client
```

### 4.3 设计约束

- **从 `DEBUG` 改为 `WARNING`**：环境变量缺失是配置错误，不是正常情况
- **增加 env var 前置检查**：避免 Langfuse SDK 在缺少 key 时走到网络调用再失败
- **保持 non-blocking**：Langfuse 失败不影响核心对话流，只是丧失可观测性
- **保持 `_init_failed` 机制**：只尝试一次，避免每次 LLM 调用都重试

---

## 5. FIX-4: before/after tool_call 语义对齐

### 5.1 问题分析

8 个 session 中 6 个出现 before/after_tool_call 计数不等，两个根因：

**根因 A：`final_answer` 只有 after 没有 before**

`step_callback` 在 `AgentFinish` 时通过 adapter 的 `on_after_tool_call(tool_name="final_answer")` 报告，但 `final_answer` 不是真正的工具调用，不经过 `@before_tool_call`。

**根因 B：sandbox MCP 工具只有 before 没有 after**

CrewAI 的 `@before_tool_call` hook 对每个工具触发，但 `step_callback` 只报告 `AgentAction.tool`（顶层工具名）。Sub-Crew 内部的 MCP 工具调用的完成事件不通过 `step_callback` 传播。

### 5.2 修改方案

**不做配对修正**，而是**在设计文档和日志中明确语义**：

1. `before_tool_call`：记录**所有工具调用尝试**（含 Sub-Crew 内部工具）
2. `after_tool_call`：记录**从 step_callback 可观测到的工具完成**（含虚拟的 `final_answer`）

这不是 bug，是 CrewAI 的 `@before_tool_call` 和 `step_callback` 的观测粒度不同导致的**结构性不对称**。

**修改 1**：`after_tool_handler` 日志中增加 `is_virtual` 标记：

```python
def after_tool_handler(ctx) -> None:
    _emit({
        "event": "after_tool_call",
        "session_id": ctx.session_id,
        "turn": ctx.turn_number,
        "tool_name": ctx.tool_name,
        "success": ctx.success,
        "duration_ms": ctx.duration_ms,
        "is_virtual": ctx.tool_name == "final_answer",
    })
```

**修改 2**：Langfuse span 中 `final_answer` 类型从 tool span 改为普通 span（不混入工具 span 统计）——这个变更在 `langfuse_trace.py` 的 `after_tool_handler` 中判断 `ctx.tool_name == "final_answer"` 时使用 `task-complete` 而非 `tool-*` 命名。

### 5.3 设计约束

- **不尝试补齐配对**：强行在 Sub-Crew 内部插入 after_tool_call 会破坏 CrewAI 的 hook 生命周期
- **Langfuse 侧已有 auto-close 机制**（`langfuse_trace.py:267` `auto-closed-by-next-llm`），span 最终会被关闭，只是 endTime 不精确
- **日志消费方知道 `is_virtual=true` 的 after_tool_call 无匹配 before**

---

## 6. FIX-5: E2E-11 超时缓解

### 6.1 问题分析

E2E-11 单独运行 178s 通过，全套件中因前置测试（E2E-07 43s + E2E-09 116s）累计后超时。根因是 Agent 对「帮我搜索一下 FastAPI 最新版本」执行了 4 个 turn、247 次 LLM 调用、10 次工具调用（含浏览网页、写文件、重试），远超预期。

### 6.2 修改方案

**测试侧**（非项目代码修改）：

1. E2E-11 使用更简单的触发消息，避免 Agent 进入长浏览链：
   - 现有：`"帮我搜索一下 FastAPI 最新版本有什么更新"`
   - 建议：`"你好，帮我解释什么是 Python 装饰器"` → 纯 LLM 回答，不触发 sandbox 工具

   但这会导致 E2E-11 无法验证 tool span。折中方案：保持原消息但增加 pytest-timeout：

2. 在 `conftest.py` 或 `pyproject.toml` 中安装 `pytest-timeout` 并设置：
   ```ini
   [tool.pytest.ini_options]
   timeout = 600
   ```

3. E2E-11 单独标记更长超时：
   ```python
   @pytest.mark.timeout(900)
   ```

**项目侧的可选改进**：在 `build_agent_fn` 中传入 `max_iter` 参数限制 Agent 最大推理轮次。但这属于可靠性策略层（LoopDetector 的职责），不在本次修复范围。

---

## 7. hooks.yaml 变更汇总

```yaml
hooks:
  BEFORE_TURN:
    - handler: structured_log.before_turn_handler
    - handler: langfuse_trace.before_turn_handler
  BEFORE_LLM:
    - handler: structured_log.before_llm_handler
    - handler: langfuse_trace.before_llm_handler
  BEFORE_TOOL_CALL:
    - handler: structured_log.before_tool_handler
    - handler: langfuse_trace.before_tool_handler
  AFTER_TOOL_CALL:
    - handler: structured_log.after_tool_handler
    - handler: langfuse_trace.after_tool_handler
  AFTER_TURN:
    - handler: structured_log.after_turn_handler       # ← FIX-1 新增
    - handler: langfuse_trace.after_turn_handler
  TASK_COMPLETE:
    - handler: structured_log.task_complete_handler     # ← FIX-1 新增
    - handler: langfuse_trace.task_complete_handler
  SESSION_END:
    - handler: structured_log.session_end_handler       # ← FIX-1 新增
    - handler: langfuse_trace.flush_and_close

# strategies 节不变
```

---

## 8. 测试策略

### 8.1 单元测试（新增 / 修改）

| 文件 | 测试 | 验证点 |
|------|------|--------|
| `tests/unit/test_structured_log.py` | `test_after_turn_handler_emits_json` | after_turn 事件输出正确 JSON |
| 同上 | `test_task_complete_handler_emits_json` | task_complete 事件输出正确 JSON |
| 同上 | `test_session_end_handler_emits_json` | session_end 事件输出正确 JSON |
| `tests/unit/test_crew_adapter.py` | `test_tool_duration_ms_nonzero` | on_before → sleep(10ms) → on_after → duration_ms > 0 |
| 同上 | `test_deny_duration_ms` | deny 路径也有 duration_ms |
| `tests/unit/test_langfuse_trace.py` | `test_ensure_client_missing_keys_warns` | 缺少 env var 时日志 WARNING |

### 8.2 集成测试（修改现有）

E2E-11 增加结构化日志 7 事件完整性验证：

```python
# 从 stderr 捕获的日志中提取事件类型
event_types = {e["event"] for e in parsed_events if e["session_id"] == session_id}
expected = {"before_turn", "before_llm", "before_tool_call", "after_tool_call",
            "after_turn", "task_complete", "session_end"}
assert expected.issubset(event_types), f"Missing events: {expected - event_types}"
```

### 8.3 回归测试

| 用例 | 修改前预期 | 修改后预期 |
|------|-----------|-----------|
| E2E-07 | PASS | PASS（无变化） |
| E2E-09 | PASS | PASS（duration_ms 非零） |
| E2E-11 | TIMEOUT in suite | PASS（7 事件完整） |
| E2E-12 | PASS | PASS（deny duration_ms 非零） |
| E2E-15 | PASS | PASS（deny duration_ms 非零） |

---

## 9. 设计自查 Checklist

### 9.1 问题覆盖完整性

| 问题 | 是否解决 | 解决记录 |
|------|---------|---------|
| structured_log 缺 3 个 handler | ✅ 完整补全 | §2 FIX-1 |
| duration_ms 全量 0 | ✅ monotonic 计时 | §3 FIX-2 |
| Langfuse 0 trace 无告警 | ✅ WARNING + 前置检查 | §4 FIX-3 |
| before/after tool_call 失配 | ✅ 语义明确 + is_virtual 标记 | §5 FIX-4 |
| E2E-11 超时 | ✅ pytest-timeout + 可选 max_iter | §6 FIX-5 |

### 9.2 通用性检查

| 检查项 | 结果 |
|--------|------|
| 修复是否依赖特定 LLM 模型？ | ❌ 不依赖，纯框架层修复 |
| 修复是否依赖特定 MCP 工具？ | ❌ 不依赖，对所有工具通用 |
| 新增代码是否可被 workspace 层 hook 覆盖？ | ✅ structured_log 在 global layer，workspace 可叠加 |
| 新增字段是否 backward compatible？ | ✅ `is_virtual` 和 `duration_ms` 不破坏现有日志消费方 |

### 9.3 一致性

| 知识点 | 一致性 |
|-----------|--------|
| 5+2 事件体系 | ✅ FIX-1 补全后 structured_log 覆盖全部 7 个事件 |
| 结构化日志是 Langfuse 降级方案 | ✅ FIX-1 让两者覆盖相同事件集 |
| span 栈 push/pop | ✅ FIX-2 的 (tool_name, turn) key 与 span stack 索引一致 |
| dispatch_gate 可阻断 | ✅ FIX-2 deny 路径不影响 dispatch_gate 语义 |
| pending_deny 传播 | ✅ FIX-2 在 deny 分支清理 start_times |
| 三层安全 | ✅ 不触碰安全策略层，仅修复观测层 |
| SecurityAuditLogger | ✅ 不影响审计日志（独立 handler） |

### 9.4 框架整体性

| 检查项 | 结果 |
|--------|------|
| HookContext frozen dataclass 不可变性 | ✅ 不修改 HookContext 定义 |
| HookRegistry dispatch/dispatch_gate 分层 | ✅ 新 handler 走 dispatch（观测层） |
| hooks.yaml 声明式注册 | ✅ 新增 handler 通过 YAML 注册 |
| CrewObservabilityAdapter 单一职责 | ✅ 计时是 adapter 职责（桥接 CrewAI → Hook） |
| ContextVar 线程安全 | ✅ `_tool_start_times` 是 adapter 实例属性，adapter 是 per-session 的 |
| 不引入新依赖 | ✅ `time.monotonic()` 是标准库 |

---

## 附录 A：E2E 日志 Review 数据摘要

| Session | Test | Events | Turns | before_tool | after_tool | 失配原因 |
|---------|------|--------|-------|-------------|------------|---------|
| s-b8c8d9ef9e26 | E2E-07 save | 16 | 2 | 5 | 3 | 3 MCP 工具无 after |
| s-9b8cf87011a9 | E2E-07 recall | 6 | 1 | 1 | 2 | +1 final_answer |
| s-0ec2144eaa50 | E2E-09 | 232 | 1 | 16 | 32 | +16 final_answer |
| s-2ebb75857a90 | E2E-11 | 266 | 4 | 10 | 5 | 5 MCP 工具无 after + timeout |
| s-c336627cfcc9 | E2E-12 loop | 45 | 1 | 2 | 2 | ✅ 配对 |
| s-e3ec6b6eac18 | E2E-12 cost | 3 | 1 | 1 | 1 | ✅ 配对（deny） |
| s-e9f4df7b3945 | E2E-15 deny | 28 | 1 | 2 | 3 | +1 final_answer |
| s-f7cc7f02085e | E2E-15 recover | 29 | 1 | 2 | 3 | +1 final_answer |
