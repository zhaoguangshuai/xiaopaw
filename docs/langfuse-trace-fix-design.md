# Langfuse Trace Span 修复方案

## 1. 问题总览

通过 `test_e2e_07_memory_save` 的 Langfuse trace 分析，发现以下问题：

| # | 问题 | 文件 | 根因 |
|---|------|------|------|
| P1 | `tool-agent_execution` output 为空 + 重复 span | langfuse_trace.py | after_turn_handler 提前清空 stack 且不加 closed_spans |
| P2 | `tool-skill_loader` output 为空 | main_crew.py | step_callback 用 `AgentAction.result`（不存在）|
| P3 | `llm-call` output 只有 tool 名称无参数 | langfuse_trace.py | span stack 不存储 tool_input |
| P4 | AFTER_TURN 多次触发全量清理 | langfuse_trace.py / crew_adapter.py | 中间步骤和最终收尾共用同一逻辑 |
| P5 | sub-crew tool span 不在 generation 下 | langfuse_trace.py | `_get_parent_id()` 不考虑当前 gen |
| P6 | `final_answer` 产生孤立 standalone span | main_crew.py | 虚拟工具无 before_tool_call 配对 |
| P7 | sub-crew 残留 gen/span 未关闭 | skill_loader.py | `_flush_langfuse_subcrew` 只 flush 不 cleanup |
| P8 | task_complete_handler 覆盖 root span output | langfuse_trace.py | 与 after_turn_handler 写同一个字段冲突 |

---

## 2. 修复后的期望 Span 树

```
Trace: xiaopaw-session-{sid}
  input: {message: "请记住..."}
  output: {reply: "好的，我已经记住了..."}
  └── session-{sid} (root span)
        output: {reply: "好的，我已经记住了..."}
        │
        └── tool-agent_execution
              input: {content: "请记住..."}
              output: {reply: "好的，我已经记住了...", success: true}
              │
              ├── llm-call-1 (orchestrator 决定调用 skill_loader)
              │     input: {messages: [...]}
              │     output: {action: "tool_calls", tools: [{name: "skill_loader", input: {skill_name: "memory-save", ...}}]}
              │     │
              │     └── tool-skill_loader
              │           input: {skill_name: "memory-save", task_context: "..."}
              │           output: {result: "已将用户信息保存到 user.md", success: true}
              │           │
              │           ├── llm-call-1 (sub-crew 决定写文件)
              │           │     input: {messages: [...]}
              │           │     output: {action: "tool_calls", tools: [{name: "sandbox_file_ops", input: {...}}]}
              │           │     │
              │           │     └── tool-sandbox_file_operations
              │           │           input: {path: "/workspace/user.md", content: "..."}
              │           │           output: {result: "文件已写入", success: true}
              │           │
              │           └── llm-call-2 (sub-crew 生成结果)
              │                 input: {messages: [...]}
              │                 output: "已将用户信息保存到 user.md"
              │
              ├── llm-call-2 (orchestrator 生成最终回复)
              │     input: {messages: [...]}
              │     output: "好的，我已经记住了..."
              │
              └── task-complete
                    input: "历史对话..."
                    output: "{reply: ..., used_skills: [memory-save]}"
```

关键特征：
- **严格父子关系**：generation → tool（LLM 决策 → 工具执行）
- **agent_execution 包裹整个 Crew**：从第一个 LLM 调用到 task-complete
- **sub-crew 嵌套在 skill_loader 下**：独立的 gen 计数器，共享 trace_id
- **每个 span 有完整的 input/output**：无空 output
- **无重复 span**：每个工具调用恰好一个 span

---

## 3. 根因分析与修复设计

### 3.1 核心问题：AFTER_TURN 语义混淆（P4，影响 P1/P2）

**现状**：`after_turn_handler` 每次 AFTER_TURN 都执行全量清理（关闭 gen、清空 stack、更新 trace、flush）。但 AFTER_TURN 从两个来源触发：

| 来源 | 语义 | 触发频率 |
|------|------|---------|
| step_callback → `dispatch_after_turn()` | 中间步骤完成 | 每个 tool call / final answer 各一次 |
| Runner._handle 第 196 行 | 整个请求完成 | 一次 |

**修复**：`after_turn_handler` 区分中间步骤和最终收尾。

**crew_adapter.py**：

```python
def dispatch_after_turn(self, output: str = "") -> None:
    self._registry.dispatch_gate(
        EventType.AFTER_TURN,
        HookContext(
            ...
            metadata={
                "output": _truncate(output),
                "is_intermediate": True,       # ← 新增
            },
        ),
    )

def make_step_callback(self) -> Callable:
    def callback(step):
        ...
        self._registry.dispatch_gate(
            EventType.AFTER_TURN,
            HookContext(
                ...
                metadata={
                    "output": step_output,
                    "is_intermediate": True,   # ← 新增
                },
            ),
        )
```

**runner.py**：Runner 的 AFTER_TURN 不设 `is_intermediate`（默认为 False）。

**langfuse_trace.py** `after_turn_handler`：

```python
def after_turn_handler(ctx) -> None:
    if not _ENABLED:
        return

    is_intermediate = ctx.metadata.get("is_intermediate", False)

    if is_intermediate:
        # 中间步骤：不做任何 span 管理
        # 策略 hook（cost_guard, loop_detector）仍正常触发
        return

    # 以下是最终收尾逻辑（仅 Runner 触发）
    _ensure_trace(ctx)
    output = ctx.metadata.get("reply", "") or ctx.metadata.get("output", "")

    # 关闭当前 gen
    gen_id = _gen_id_var.get("")
    if gen_id:
        ... # (现有逻辑不变)
        _gen_id_var.set("")

    # 关闭残留 stack + 加入 closed_spans（FIX P1）
    stack = list(_span_stack_var.get(()))
    if stack:
        closed = dict(_closed_spans_var.get({}))
        for entry in stack:
            closed[(entry[1], entry[2])] = entry[0]
            _enqueue(IngestionEvent_SpanUpdate(...))
        _closed_spans_var.set(closed)        # ← 新增：加入 closed_spans
        _span_stack_var.set(())

    # 更新 trace + root span + flush（不变）
    ...
    _flush_batch()
```

**影响范围**：
- `langfuse_trace.py`：after_turn_handler（~20 行改动）
- `crew_adapter.py`：dispatch_after_turn + make_step_callback（各 1 行）
- `runner.py`：无需改动（Runner 的 AFTER_TURN 不设 is_intermediate，行为不变）
- 策略 hook（cost_guard, loop_detector）：不受影响（它们监听 AFTER_TURN 事件本身，不关心 is_intermediate 标志）

---

### 3.2 step_callback 不再关闭工具 span（P2 根本修复）

**现状**：step_callback 为 `AgentAction` 调用 `on_after_tool_call`，但 `AgentAction.result` 在 CrewAI 中不存在，导致 tool_output 永远为空。

**修复**：step_callback 不再为 `AgentAction` 调用 `on_after_tool_call`。工具 span 的关闭改为由 `before_llm_handler` 的 auto-close 机制完成（该机制从 prompt_messages 中提取真实工具结果）。

**main_crew.py** `_make_step_callback`：

```python
async def _callback(step_output: Any) -> None:
    if isinstance(step_output, AgentAction) and step_output.thought:
        try:
            await sender.send_thinking(routing_key, step_output.thought[:200])
        except Exception:
            pass

    adapter = get_current_adapter()
    if not adapter:
        return

    # 删除：不再为 AgentAction 调用 on_after_tool_call
    # 删除：不再为 AgentFinish 调用 on_after_tool_call("final_answer")

    # 保留：AFTER_TURN（中间步骤）仍触发，用于策略 hook
    step_text = ""
    if isinstance(step_output, AgentAction):
        step_text = str(step_output.text or step_output.thought or "")
    elif isinstance(step_output, AgentFinish):
        step_text = str(getattr(step_output, "output", "") or "")
    adapter.dispatch_after_turn(output=step_text[:2000])

    if adapter._pending_deny:
        pending = adapter._pending_deny
        adapter._pending_deny = None
        raise pending
```

**skill_crew.py** `_make_subcrew_step_callback`：同样删除 `on_after_tool_call` 调用。

**工具 span 新生命周期**：

```
before_tool_call  →  span create, push stack
                     (tool 执行)
before_llm (next) →  auto-close: 从 prompt_messages 提取结果, pop stack, 加入 closed_spans
                     OR
after_turn (final) → 收尾关闭残留 stack
                     OR  
after_tool (Runner) → agent_execution 由 Runner 显式关闭
```

---

### 3.3 before_llm_handler 选择性 auto-close（P1 根本修复）

**现状**：`before_llm_handler` auto-close 时关闭 stack 中的**所有**条目。这导致 `agent_execution`（外层包裹 span）也被提前关闭。

**修复**：只关闭在 prompt_messages 中有对应 tool result 的 span。没有 result 的 span（如 `agent_execution`）保留在 stack 中。

**langfuse_trace.py** `before_llm_handler` auto-close 逻辑：

```python
prev_gen_id = _gen_id_var.get("")
if prev_gen_id:
    stack = list(_span_stack_var.get(()))
    tool_results = _extract_recent_tool_results(
        ctx.metadata.get("prompt_messages", [])
    )

    remaining_stack = []
    closed_entries = []

    closed = dict(_closed_spans_var.get({}))
    for entry in stack:
        span_id, tool_name, turn_num = entry[0], entry[1], entry[2]
        if tool_name in tool_results:
            # 有 tool result → 关闭并记录 output
            span_output = {"result": tool_results.pop(tool_name)}
            closed[(tool_name, turn_num)] = span_id
            closed_entries.append(entry)
            _enqueue(IngestionEvent_SpanUpdate(
                id=span_id,
                output=span_output,
                end_time=_now(),
                metadata={"phase": "auto-closed-by-next-llm"},
            ))
        else:
            # 无 tool result → 保留在 stack
            remaining_stack.append(entry)

    _closed_spans_var.set(closed)
    _span_stack_var.set(tuple(remaining_stack))

    # gen output 只包含被关闭的工具（见 3.5）
    gen_output = None
    if closed_entries:
        gen_output = {
            "action": "tool_calls",
            "tools": [
                {"name": e[1], "input": e[3] if len(e) > 3 else {}}
                for e in closed_entries
            ],
        }

    # 关闭前一个 gen
    close_kwargs = {"id": prev_gen_id, "end_time": _now()}
    if gen_output:
        close_kwargs["output"] = gen_output
    _enqueue(IngestionEvent_GenerationUpdate(**close_kwargs))
    _gen_id_var.set("")
```

**结果**：`agent_execution` 不在 prompt_messages 中有 tool result → 保留在 stack → 后续 gen 的 parent 正确指向 agent_execution。

---

### 3.4 tool span parent 改为当前 generation（P5）

**现状**：`_get_parent_id()` 只看 span stack 和 root，不考虑当前 generation。导致 tool span 和 generation 是平级兄弟。

**修复**：优先返回当前 generation ID。

```python
def _get_parent_id() -> str:
    # 优先级：当前 gen > span stack top > root
    gen_id = _gen_id_var.get("")
    if gen_id:
        return gen_id
    stack = _span_stack_var.get(())
    if stack:
        return stack[-1][0]
    return _root_span_id_var.get("")
```

**效果**：tool span 嵌套在触发它的 generation 下：

```
llm-call-1 (decides to call skill_loader)
  └── tool-skill_loader (child of llm-call-1)
```

**注意**：`before_llm_handler` 创建新 gen 时，应在 push 新 gen **之前**调用 `_get_parent_id()`，这样新 gen 的 parent 是 span stack top（如 agent_execution），而不是自己。新 gen 的 parent 逻辑需要单独处理：

```python
# 新 gen 的 parent = span stack top 或 root（不是当前 gen）
stack = _span_stack_var.get(())
new_gen_parent = stack[-1][0] if stack else _root_span_id_var.get("")

gen_id = _uid()
_gen_id_var.set(gen_id)

_enqueue(IngestionEvent_GenerationCreate(
    ...
    parent_observation_id=new_gen_parent,  # ← 用独立变量，不用 _get_parent_id()
    ...
))
```

---

### 3.5 span stack 存储 tool_input（P3）

**现状**：stack 条目为 `(span_id, tool_name, turn_number)`，auto-close 时 gen output 只有 tool 名称。

**修复**：扩展为 `(span_id, tool_name, turn_number, tool_input)`。

**before_tool_handler**：

```python
old_stack = _span_stack_var.get(())
_span_stack_var.set((*old_stack, (span_id, ctx.tool_name, ctx.turn_number, tool_input)))
```

**before_llm_handler** gen output 改为：

```python
gen_output = {
    "action": "tool_calls",
    "tools": [
        {"name": e[1], "input": e[3]}
        for e in closed_entries
    ],
}
```

**受影响的引用点**（所有读取 stack 条目的地方）：
- `after_tool_handler`：`stack[i][1]`, `stack[i][2]` → 不变
- `after_turn_handler`：`entry[0]`, `entry[1]`, `entry[2]` → 不变
- `flush_and_close`：`entry[0]` → 不变
- `_get_parent_id()`：`stack[-1][0]` → 不变
- `_get_langfuse_parent_span_id()`（skill_loader.py）：`stack[-1][0]` → 不变

只有 `before_llm_handler` 需要读 `entry[3]`，其他位置按索引访问 [0][1][2] 不受影响。

---

### 3.6 sub-crew 清理（P7）

**现状**：`_flush_langfuse_subcrew()` 只 flush batch，不关闭残留 gen/span。

**修复**：新增 `subcrew_cleanup()` 函数，在 flush 前关闭残留对象。

**langfuse_trace.py** 新增：

```python
def subcrew_cleanup() -> None:
    """Close remaining gen/spans from sub-crew context, then flush. No session_end."""
    if not _ENABLED:
        return

    gen_id = _gen_id_var.get("")
    if gen_id:
        _enqueue(IngestionEvent_GenerationUpdate(
            ..., body=UpdateGenerationBody(id=gen_id, end_time=_now())
        ))
        _gen_id_var.set("")

    stack = _span_stack_var.get(())
    if stack:
        for entry in stack:
            _enqueue(IngestionEvent_SpanUpdate(
                ..., body=UpdateSpanBody(id=entry[0], end_time=_now())
            ))
        _span_stack_var.set(())

    _flush_batch()
```

**skill_loader.py** 修改 `_flush_langfuse_subcrew`：

```python
def _flush_langfuse_subcrew() -> None:
    try:
        from shared_hooks.langfuse_trace import subcrew_cleanup
        subcrew_cleanup()
    except ImportError:
        pass
```

---

### 3.7 task_complete_handler 不覆盖 root span（P8）

**现状**：`task_complete_handler` 第 604-619 行更新 root span 的 input/output，与 `after_turn_handler` 设置的 reply 冲突。

**修复**：删除 root span 更新，只保留独立的 `task-complete` span。

```python
def task_complete_handler(ctx) -> None:
    ...
    # 创建 task-complete span（不变）
    _enqueue(IngestionEvent_SpanCreate(
        ..., name="task-complete", input=task_desc, output=raw_output, ...
    ))

    # 删除：不再更新 root span
    # if root_id and (task_desc or raw_output):
    #     _enqueue(IngestionEvent_SpanUpdate(id=root_id, ...))
```

---

### 3.8 删除 final_answer 虚拟工具（P6）

**现状**：step_callback 对 `AgentFinish` 调用 `on_after_tool_call("final_answer")`，但 `final_answer` 没有 `before_tool_call` 配对，导致 `after_tool_handler` 走 fallback 创建 standalone span。

**修复**：在 3.2 中已删除，step_callback 不再对任何 step_output 调用 `on_after_tool_call`。AgentFinish 的 output 由 final AFTER_TURN 的 gen close + trace update 捕获。

---

## 4. 修改文件清单

| 文件 | 改动 | 行数估算 |
|------|------|---------|
| `shared_hooks/langfuse_trace.py` | 3.1 after_turn 区分中间/最终 + 3.3 选择性 auto-close + 3.4 parent 逻辑 + 3.5 stack 扩展 + 3.6 subcrew_cleanup + 3.7 删除 root 覆盖 | ~80 行 |
| `xiaopaw/hook_framework/crew_adapter.py` | 3.1 dispatch_after_turn 加 is_intermediate + make_step_callback 加 is_intermediate | ~4 行 |
| `xiaopaw/agents/main_crew.py` | 3.2 step_callback 删除 on_after_tool_call | ~-10 行 |
| `xiaopaw/agents/skill_crew.py` | 3.2 subcrew step_callback 同步修改 | ~-10 行 |
| `xiaopaw/tools/skill_loader.py` | 3.6 调用 subcrew_cleanup 替代 _flush_batch | ~2 行 |

**不改动的文件**：
- `runner.py` — Runner 的 AFTER_TURN 不设 `is_intermediate`，天然是 final，无需改动
- `hook_framework/registry.py` — EventType 不变，HookContext 不变
- `shared_hooks/hooks.yaml` — 事件注册不变

---

## 5. 事件流对照（修复前 vs 修复后）

### 修复前

```
BEFORE_TURN
BEFORE_TOOL("agent_execution")     → span create, push stack
  BEFORE_LLM                       → gen-1 create (parent=ae)
  BEFORE_TOOL("skill_loader")      → span create, push stack
    [sub-crew executes]
  step_callback:
    AFTER_TOOL("skill_loader", "")  → pop stack, update span (EMPTY output) ← P2
    AFTER_TURN                      → close gen-1, close stack(ae!), flush ← P1/P4
  BEFORE_LLM                       → gen-2 (parent=ROOT, ae已被清) ← P1
  step_callback:
    AFTER_TOOL("final_answer")      → standalone span ← P6
    AFTER_TURN                      → close gen-2, flush ← P4
  TASK_COMPLETE                     → update root span ← P8
AFTER_TOOL("agent_execution", reply) → ae not found → DUPLICATE span ← P1
AFTER_TURN (Runner)                 → flush again ← P4
SESSION_END
```

### 修复后

```
BEFORE_TURN
BEFORE_TOOL("agent_execution")     → span create, push stack
  BEFORE_LLM                       → gen-1 create (parent=ae)
  BEFORE_TOOL("skill_loader")      → span create, push stack (under gen-1)
    [sub-crew executes, sub-crew cleanup closes sub spans]
  step_callback:
    AFTER_TURN (intermediate)       → langfuse NO-OP ← FIX P4
  BEFORE_LLM                       → auto-close:
                                       skill_loader has result → close with output ← FIX P2
                                       agent_execution no result → keep in stack ← FIX P1
                                       gen-1 output: {tools: [{name: skill_loader, input: {...}}]} ← FIX P3
                                       gen-2 create (parent=ae, still in stack) ← FIX P1
  step_callback:
    AFTER_TURN (intermediate)       → langfuse NO-OP ← FIX P6
  TASK_COMPLETE                     → task-complete span only ← FIX P8
AFTER_TOOL("agent_execution", reply) → find ae in stack → close with output ← FIX P1
AFTER_TURN (final, Runner)          → close gen-2, update trace, flush ← FIX P4
SESSION_END
```

---

## 6. 测试策略

### 6.1 需更新的单元测试

`tests/unit/shared_hooks/test_langfuse_autoclose.py`：

- `TestAfterTurnAutoCloseSpans`：需增加 intermediate 和 final 两个 path 的测试
- `TestBeforeLlmAutoCloseSpans`：需增加"保留无 result 的 stack entry"的测试
- `TestSubCrewFlowSimulation`：需更新 full flow 预期（不再有 on_after_tool_call 从 step_callback）
- 新增：`TestSelectiveAutoClose` — 验证 agent_execution 不被 auto-close
- 新增：`TestParentHierarchy` — 验证 tool span parent 是当前 gen
- 新增：`TestSubcrewCleanup` — 验证 sub-crew gen/span 正确关闭

### 6.2 需更新的 E2E 测试

`tests/e2e/test_e2e_10_langfuse_trace.py`：

- 增加 span 层级关系验证（generation → tool 父子关系）
- 增加 output 非空验证
- 增加无重复 span 验证

### 6.3 新增集成测试

`tests/integration/test_trace_span_tree.py`（可选）：

- 模拟完整 memory-save 流程
- 验证 span 树结构精确匹配期望
- 验证 agent_execution/skill_loader 的 output 非空

---

## 7. 风险与约束

| 风险 | 缓解 |
|------|------|
| 中间 AFTER_TURN no-op 影响策略 hook | 策略 hook 在 HookRegistry 层面仍正常触发，langfuse handler 的 no-op 不影响其他 handler |
| 选择性 auto-close 漏掉需要关闭的 span | `after_turn_handler(final)` 和 `flush_and_close` 作为兜底，确保所有 span 最终关闭 |
| `_get_parent_id()` 改用 gen_id 改变所有 trace 的父子关系 | 这是有意为之的改进，符合 OpenTelemetry 语义（LLM 决策 → 工具执行） |
| step_callback 不再 fire AFTER_TOOL_CALL | 验证 loop_detector / retry_tracker 是否只依赖 BEFORE_TOOL_CALL（它们的 check 逻辑是在 before 还是 after）|
| span stack 增加 tool_input 字段 | 内存影响极小（工具 input 已经在 span 的 input 字段中存储），stack 只是多了一个引用 |

---

## 8. Architect Review 反馈与修订

### B1: 重复 tool_name 匹配失败（BLOCKING）

**问题**：`_extract_recent_tool_results` 返回 `dict[str, str]`，重复 tool_name（如两次 `bash`）只保留最后一个。
**修复**：改为返回 `list[tuple[str, str]]`，保留所有结果。`before_llm_handler` auto-close 使用位置匹配：
```python
used_indices: set[int] = set()
for entry in stack:
    for i, (rname, rcontent) in enumerate(tool_results):
        if i not in used_indices and rname == tool_name:
            matched_content = rcontent
            used_indices.add(i)
            break
```
**状态**：✅ 已实现

### B2: `_get_parent_id()` 修改破坏 gen parent（BLOCKING）

**问题**：如果只有一个 `_get_parent_id()` 且优先返回 gen_id，新 gen 的 parent 会指向自己。
**修复**：拆为两个函数：
- `_get_tool_parent_id()` — 优先级：current gen > stack top > root
- `_get_gen_parent_id()` — 优先级：stack top > root（永远不返回当前 gen）
**状态**：✅ 已实现

### B3: loop_detector 中间 AFTER_TURN 触发误报（BLOCKING）

**验证结果**：非问题。`is_intermediate` 标志仅在 `langfuse_trace.after_turn_handler` 内检查并 early return。
其他 handler（loop_detector, cost_guard）仍正常触发，因为它们在 `HookRegistry.dispatch_gate()` 层面接收所有 AFTER_TURN 事件。
loop_detector 基于 output hash 去重，中间步骤 output 各不相同（不同 thought/tool result），不会触发阈值。
**状态**：✅ 已验证，无需额外修改

### N5: task-complete parent 改为 agent_execution

**修改**：`task_complete_handler` 使用 stack top 作为 parent（TASK_COMPLETE 在 AFTER_TOOL(agent_execution) 之前触发，agent_execution 仍在栈中）。
**状态**：✅ 已实现

---

## 9. 实施与验证结果

### 修改文件

| 文件 | 改动 |
|------|------|
| `shared_hooks/langfuse_trace.py` | 选择性 auto-close + parent ID 拆分 + 4-tuple stack + subcrew_cleanup + 中间 AFTER_TURN no-op + task-complete parent 修复 + 删除 root span 覆盖 |
| `xiaopaw/hook_framework/crew_adapter.py` | dispatch_after_turn + make_step_callback 加 `is_intermediate: True` |
| `xiaopaw/agents/main_crew.py` | step_callback 删除 on_after_tool_call（AgentAction + AgentFinish） |
| `xiaopaw/agents/skill_crew.py` | subcrew step_callback 同步删除 on_after_tool_call |
| `xiaopaw/tools/skill_loader.py` | 调用 subcrew_cleanup 替代 _flush_batch |

### 测试结果

- **单元测试**：29/29 PASS（含新增 TestParentHierarchy, TestSubcrewCleanup, TestTaskCompleteParent, TestSelectiveAutoClose）
- **全量 unit + integration**：228/228 PASS
- **E2E test_e2e_10_langfuse_trace**：PASS（simple_conversation_trace 验证通过）
- **E2E test_e2e_07_memory_save**：Steps 1-2 PASS（trace 流程正确），Step 3 cross-session recall FAIL（pre-existing 记忆持久化问题，与 trace 修复无关）
