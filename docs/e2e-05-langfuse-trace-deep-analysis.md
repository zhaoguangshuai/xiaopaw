# E2E-05 Langfuse Trace 深度分析报告

**Trace ID**: `54bae50cfe7f4771` | **Session**: `s-752a5ba29c7f` | **分析时间**: 2026-05-02

## 一、总体概况

| 指标 | 值 | 评价 |
|------|-----|------|
| Observation 总数 | 21 | ✅ |
| GENERATION | 8 | ✅ |
| SPAN(tool) | 10 | ✅ |
| SPAN(lifecycle) | 3 | ✅ |
| 有 Input | 18/21 (86%) | ⚠️ 3条缺失 |
| 有 Output | 13/21 (62%) | ❌ 8条缺失 |
| 有 Usage/Token | 0/21 (0%) | ❌ 全部缺失 |
| 总延迟 | 181.0s | - |

---

## 二、逐条 Observation 审计

### Obs #1: root span `session-s-752a5ba29c7f` ✅

| 字段 | 状态 | 内容 |
|------|------|------|
| Input | ✅ | 用户消息 + 系统指令（157 chars） |
| Output | ✅ | 最终回复（514 chars） |
| Duration | 181.0s | 覆盖完整生命周期 |
| Metadata | ✅ | `source: xiaopaw-v2`, `session_id` |

**结论**: Root span 完整，input/output/metadata 齐全。

---

### Obs #2: `tool-agent_execution` (opened) ⚠️

| 字段 | 状态 | 问题 |
|------|------|------|
| Input | ✅ | `{"content": "帮我搜索一下..."}` |
| Output | ❌ NULL | auto-closed-by-after-turn，未携带 output |
| Phase | auto-closed-by-after-turn | 被 after_turn_handler 自动关闭 |

**问题**: 这是 `agent_execution` 工具的 **开始 span**（before_tool_handler 创建）。因为 agent_execution 是长时间运行的包装工具（179.2s），在其执行期间触发了大量子事件。`after_turn_handler` 发现它还在 span stack 中，直接关闭但 **不携带 output**。

与 Obs #20（同名 span，phase=completed）形成重复——`after_tool_handler` 找不到匹配的 stack 条目，创建了一个全新的 span。

---

### Obs #3: `llm-call-1` (Main Crew) ✅

| 字段 | 状态 | 内容 |
|------|------|------|
| Input | ✅ | system prompt（XiaoPaw 身份 + Soul + Agent 配置）+ user task |
| Output | ✅ | `{"action": "tool_calls", "tools": ["agent_execution", "skill_loader"]}` |
| Model | qwen3-max | ✅ |
| Duration | 4.6s | ✅ 正常 |
| Usage | ❌ | input=0, output=0, total=0 |
| Parent | tool-agent_execution | ✅ 正确嵌套 |

**分析**: Main Crew 的第一次 LLM 调用，正确识别了搜索意图，决策调用 `skill_loader(baidu_search)`。Output 是 `before_llm_handler` 自动合成的"工具调用摘要"，不是实际 LLM 输出文本。

**问题**: Output 格式是 `{"action": "tool_calls", "tools": [...]}` —— 这是 langfuse_trace.py 的 `before_llm_handler` 在关闭前一个 GENERATION 时合成的。**丢失了实际的 LLM 响应文本**（Function Calling 的 JSON 请求体）。

---

### Obs #4: `tool-skill_loader` ⚠️

| 字段 | 状态 | 问题 |
|------|------|------|
| Input | ✅ | `{"skill_name": "baidu_search", "task_context": "{\"query\": \"Python 3.13 新特性\"}"}` |
| Output | ❌ NULL | auto-closed-by-after-turn |
| Duration | 174.8s | Sub-Crew 全程时间 |
| Parent | tool-agent_execution | ✅ |

**问题**: 与 Obs #2 相同原因——skill_loader 是长时间包装工具，在 after_turn 时被自动关闭，output 丢失。Sub-Crew 的返回值（搜索结果 JSON）未记录在此 span。

---

### Obs #5-16: Sub-Crew 执行链 ⚠️⚠️

这是 Sub-Crew（baidu_search 专家）的完整执行过程，**全部 parentId 指向 root span**。

#### 层级扁平化问题

| Obs | Name | 实际 Parent | 期望 Parent |
|-----|------|-------------|-------------|
| #5-16 | Sub-Crew 全部 obs | `session-s-752a5ba29c7f` (root) | `tool-skill_loader` |

**根因**: `_reset_langfuse_contextvars()` (skill_loader.py:21) 保留了 `_trace_id_var` 和 `_root_span_id_var`，但 `_root_span_id_var` 指向的是 root session span，不是 skill_loader span。Sub-Crew 的所有 observation 通过 `_get_parent_id()` 回退到 `_root_span_id_var`，直接挂在 root 下面。

**影响**: Langfuse UI 中看到的树是**两层扁平结构**（root → 所有子节点），而不是**三层嵌套结构**（root → agent_execution → skill_loader → Sub-Crew obs）。

#### Sub-Crew 各步骤详解

| Obs | Type | Name | Input | Output | Duration | 说明 |
|-----|------|------|-------|--------|----------|------|
| #5 | GEN | llm-call-2 | ✅ Skill 指令 + 任务 | ✅ →bash | 5.6s | 决策：先创建输出目录 |
| #6 | SPAN | sandbox_bash | ✅ `mkdir -p .../outputs` | ❌ NULL | 0.8s | 创建目录，无 output |
| #7 | GEN | llm-call-3 | ✅ 含历史消息 | ✅ →bash | 6.7s | 决策：执行搜索脚本 |
| #8 | SPAN | sandbox_bash | ✅ `search.py --query ... --top_k 20` | ❌ NULL | 2.9s | **第一次搜索**，无 output |
| #9 | GEN | llm-call-4 | ✅ 含搜索结果 | ✅ →file_ops | **131.2s** ⚠️ | **瓶颈**：处理17条结果 |
| #10 | SPAN | sandbox_file_ops | ✅ write 12409 chars JSON | ❌ NULL | 0.2s | 写入搜索结果文件 |
| #11 | GEN | llm-call-5 | ✅ 含 tool result | ✅ →bash | 8.2s | 决策：二次搜索+重定向 |
| #12 | SPAN | sandbox_bash | ✅ `search.py ... > .../raw.json` | ❌ NULL | 2.9s | **第二次搜索** |
| #13 | GEN | llm-call-6 | ✅ 含 tool result | ✅ →file_ops | 6.5s | 决策：读取结果文件 |
| #14 | SPAN | sandbox_file_ops | ✅ read path | ❌ NULL | 0.2s | 读取原始结果 |
| #15 | GEN | llm-call-7 | ✅ **含报错** | ✅ JSON | 5.0s | 处理错误后完成 |
| #16 | SPAN | final_answer | ❌ NULL | ✅ result | 0.0s | Sub-Crew 完成 |

---

### Obs #17-18: Main Crew 综合回复 ✅

| Obs | Type | Name | Input | Output | Duration |
|-----|------|------|-------|--------|----------|
| #17 | GEN | llm-call-1 | ✅ system+user+Sub-Crew结果 | ✅ **完整用户回复** | 10.7s |
| #18 | SPAN | final_answer | ❌ input | ✅ reply | 0.0s |

**分析**: Main Crew 收到 Sub-Crew 返回的搜索结果后，通过第二次 LLM 调用（turn 2）综合生成用户友好的回复。**这里的 output 是唯一一个包含实际 LLM 回复文本的 GENERATION**。

---

### Obs #19-21: 生命周期 Span

| Obs | Name | Input | Output | 说明 |
|-----|------|-------|--------|------|
| #19 | task-complete | ✅ task 描述 | ✅ reply | 正常 |
| #20 | tool-agent_execution (dup) | ✅ | ✅ | after_tool 补偿 span |
| #21 | session_end | ❌ | ❌ | 仅标记，正常 |

---

## 三、问题清单（按严重度排序）

### P0: Token Usage 全部为零

**现象**: 全部 8 个 GENERATION 的 usage 均为 `{input: 0, output: 0, total: 0}`

**根因**: `after_turn_handler` 中有 usage 上报逻辑（langfuse_trace.py:427-432），但只在 `ctx.input_tokens` 和 `ctx.output_tokens` 非零时触发。CrewAI 的 Hook 框架 context 不传递 token 计数。

**影响**:
- Langfuse 无法计算成本（totalCost = 0）
- 无法做 token 消耗分析和优化
- 无法设置基于 token 的告警

**修复建议**:
```python
# 方案 A: 从 CrewAI callback 获取 token count
# CrewAI 1.10+ 在 task callback 中暴露 token_usage
# 方案 B: 从 Qwen API 响应头中获取 usage
# DashScope API 返回 usage.input_tokens / usage.output_tokens
```

---

### P1: 工具 Span Output 大面积缺失（7/10 = 70%）

**现象**: 所有 `auto-closed-by-next-llm` 和 `auto-closed-by-after-turn` 的工具 span 均无 output

**受影响的 span**:
| Obs | Tool | Phase | 丢失内容 |
|-----|------|-------|---------|
| #2 | agent_execution | after-turn | 最终回复文本 |
| #4 | skill_loader | after-turn | Sub-Crew 搜索结果 JSON |
| #6 | sandbox_bash (mkdir) | next-llm | 命令执行结果 |
| #8 | sandbox_bash (search) | next-llm | **搜索 API 原始返回** |
| #10 | sandbox_file_ops (write) | next-llm | 写入确认 |
| #12 | sandbox_bash (search2) | next-llm | 二次搜索结果 |
| #14 | sandbox_file_ops (read) | next-llm | 读取的文件内容 |

**根因**: `before_llm_handler` (langfuse_trace.py:204-250) 在下一次 LLM 调用开始时，遍历 span stack 并关闭所有 open span，但**只设置 end_time 和 metadata，不设置 output**。之后 `after_tool_handler` 执行时，stack 已清空，找不到匹配条目。

**事件顺序**:
```
before_tool_call(X) → span 入栈
  [工具执行中...]
before_llm(Y) → 清空栈，关闭 span（❌ 无 output）
after_tool_call(X) → 栈已空，找不到 X → 创建新 span（有 output 但是重复的）
```

**影响**: 在 Langfuse UI 中，无法看到大部分工具的执行结果。搜索 API 返回了什么、文件写入是否成功，这些关键信息全部丢失。

**修复建议**:
```python
# before_llm_handler 中不直接关闭 span，改为标记 pending_close
# after_tool_handler 检查 pending_close 标记并携带 output 关闭
# 或：在 span stack 中缓存 after_tool 的 output，延迟到 before_llm 时一起写入
```

---

### P2: Sub-Crew 层级扁平化

**现象**: Sub-Crew 的 12 个 observation（#5-#16）全部 parentId 指向 root span，而非 `tool-skill_loader` span

**期望层级**:
```
session-root
  └─ tool-agent_execution
       └─ llm-call-1 (Main)
       └─ tool-skill_loader
            └─ llm-call-2 (Sub)
            └─ tool-sandbox_bash (mkdir)
            └─ llm-call-3 (Sub)
            └─ tool-sandbox_bash (search)
            └─ ...
```

**实际层级**:
```
session-root
  ├─ tool-agent_execution
  │    ├─ llm-call-1 (Main)
  │    └─ tool-skill_loader
  │         ├─ llm-call-1 (Main, turn 2)   ← 只有这两个在 skill_loader 下
  │         └─ tool-final_answer
  ├─ llm-call-2 (Sub)      ← 扁平挂在 root 下
  ├─ tool-sandbox_bash
  ├─ llm-call-3 (Sub)
  ├─ tool-sandbox_bash
  ├─ ...（全部扁平）
```

**根因**: `_reset_langfuse_contextvars()` 重置了 `_span_stack_var` 但保留了 `_root_span_id_var`。`_get_parent_id()` 在 stack 为空时 fallback 到 `_root_span_id_var`，指向 root session span。

**修复建议**:
```python
def _reset_langfuse_contextvars(parent_span_id: str = "") -> None:
    # 如果传入 skill_loader 的 span_id，设为 Sub-Crew 的 root
    if parent_span_id:
        _root_span_id_var.set(parent_span_id)
    _gen_id_var.set("")
    _gen_count_var.set(0)
    _tool_count_var.set(0)
    _span_stack_var.set(())
```

---

### P3: GENERATION Output 是合成的"工具调用摘要"，非实际 LLM 响应

**现象**: 8 个 GENERATION 中，6 个的 output 是 `{"action": "tool_calls", "tools": [...]}`

| Obs | Output 类型 | 实际内容 |
|-----|------------|---------|
| #3 | ACTION→tools | `["agent_execution", "skill_loader"]` |
| #5 | ACTION→tools | `["localhost_8029_mcp_sandbox_execute_bash"]` |
| #7 | ACTION→tools | `["localhost_8029_mcp_sandbox_execute_bash"]` |
| #9 | ACTION→tools | `["localhost_8029_mcp_sandbox_file_operations"]` |
| #11 | ACTION→tools | `["localhost_8029_mcp_sandbox_execute_bash"]` |
| #13 | ACTION→tools | `["localhost_8029_mcp_sandbox_file_operations"]` |
| #15 | **真实 JSON** | `{"errcode": 0, "message": "搜索任务成功完成。", ...}` |
| #17 | **真实回复** | 完整的用户可读回复（547 chars） |

**根因**: 同 P1——`before_llm_handler` 关闭前一个 GENERATION 时，将当前 span stack 中的工具名作为 output。实际的 LLM response（Function Calling JSON、reasoning 文本）未被捕获。

**影响**: 无法在 Langfuse 中审查每次 LLM 调用的具体输出，只能看到"调了什么工具"。对 prompt 调优和质量分析造成障碍。

---

### P4: tool-agent_execution 重复 Span

**现象**: `tool-agent_execution` 出现 2 次
- Obs #2: before_tool 创建，auto-closed（无 output）
- Obs #20: after_tool 补偿创建（有 output，但 duration=0）

**影响**: Langfuse UI 中同一工具显示两条记录。Obs #2 有正确 duration（179.2s）但无 output；Obs #20 有 output 但 duration=0。信息被拆分到两条记录。

---

### P5: Sub-Crew 遇到工具调用错误（已自愈）

**现象**: Obs #15（llm-call-7）的 input 中包含工具报错：
```
Error executing tool file_operations: 1 validation error for file_operationsArguments
content
  Input should be a valid string [type=string_type, input_value={'errcode': 0, ...}, input_type=dict]
```

**分析**: Sub-Crew 在 llm-call-6 后尝试调用 `file_operations(action=read)`，返回的内容是 dict 而非 string，触发 Pydantic 校验错误。LLM 在 llm-call-7 中看到错误后，**正确选择了跳过并提交 final_answer**。

**影响**: 功能正常（自愈），但增加了一次多余的 LLM 调用和延迟。且错误未在 Langfuse span 中被标记为 `level=ERROR`，只是混在 LLM input 的聊天历史中。

---

### P6: final_answer Span 缺少 Input

**现象**: Obs #16 和 #18 的 `tool-final_answer` 均 `input: ❌ NULL`

**根因**: `before_tool_handler` 中 `tool_input = dict(ctx.tool_input) if ctx.tool_input else {}`——final_answer 的 tool_input 可能为空或未被传递。

---

## 四、GENERATION Input 质量分析

| Obs | 有 System Prompt | 有 User Message | 有 History | 有 Tool Results |
|-----|-----------------|----------------|-----------|----------------|
| #3 (Main llm-1) | ✅ XiaoPaw 完整 prompt | ✅ 用户消息 | - | - |
| #5 (Sub llm-2) | ✅ Skill 执行指令 | ✅ 任务 JSON | - | - |
| #7 (Sub llm-3) | ✅ (同上) | ✅ | - | ✅ mkdir result |
| #9 (Sub llm-4) | ✅ (同上) | ✅ | - | ✅ search result |
| #11 (Sub llm-5) | ❌ 无 system | ✅ "Analyze..." | ✅ assistant+tool | ✅ |
| #13 (Sub llm-6) | ❌ 无 system | ✅ "Analyze..." | ✅ | ✅ search result |
| #15 (Sub llm-7) | ❌ 无 system | ✅ "Analyze..." | ✅ | ✅ **含错误** |
| #17 (Main llm-1 t2) | ✅ XiaoPaw prompt | ✅ 用户消息 | - | ✅ Sub-Crew 结果 |

**发现**: llm-call-5/6/7 的 input 中 **system prompt 缺失**，只有 user/assistant/tool 消息交替。这是 CrewAI 的正常行为——在同一 task 的多轮 tool-use 循环中，system prompt 只在第一次调用时传入，后续的 "Analyze the tool result..." 消息直接追加到对话历史。

但对于 Langfuse 分析来说，**这意味着 llm-call-5/6/7 的 input 不包含完整上下文**，需要回溯到 llm-call-2 才能看到 system prompt。

---

## 五、调用流程符合预期性分析

### ✅ 符合预期的行为

1. **意图路由正确**: Main Crew 识别"搜索"意图 → 调用 skill_loader(baidu_search)
2. **Skill 加载正确**: SkillLoaderTool 读取 SKILL.md → 创建 Sub-Crew
3. **Sandbox 隔离正确**: 所有命令在 `localhost:8029/mcp` sandbox 中执行
4. **搜索脚本调用正确**: `search.py --query "Python 3.13 新特性" --top_k 20`
5. **文件操作正确**: mkdir → search → write → search2 → read → final_answer
6. **错误自愈**: llm-call-7 正确处理了 Pydantic 校验错误
7. **最终综合正确**: Main Crew 将 Sub-Crew 结构化结果转为用户友好文本

### ⚠️ 不符合预期或可优化的行为

| # | 问题 | 严重度 | 说明 |
|---|------|--------|------|
| 1 | Sub-Crew 执行了 **两次搜索** | 低 | 第一次 search.py 直接执行，第二次加了 `> raw.json` 重定向。重复调用浪费约 3s + 一次 LLM 调用 |
| 2 | llm-call-4 耗时 **131.2s** | 高 | 占总时间 72%。输入包含 17 条搜索结果全文，LLM 需要处理大量文本并生成 12409 chars 的 JSON 输出 |
| 3 | file_operations 校验错误 | 低 | Sub-Crew 传了 dict 而非 string，触发 Pydantic 错误。虽然自愈但浪费一次 LLM 调用 |
| 4 | Sub-Crew 共 **7 次 LLM 调用** | 中 | 对于一个"搜索+返回结果"的任务来说，理想应该是 2-3 次（搜索 → 整理 → final_answer） |

---

## 六、修复优先级建议

| 优先级 | 问题 | 修复工作量 | 影响范围 |
|--------|------|-----------|---------|
| **P0** | Token Usage 全零 | 中 | 成本分析、告警、dashboard |
| **P1** | 工具 Output 缺失 70% | 高 | 调试、质量分析、trace 可读性 |
| **P2** | Sub-Crew 层级扁平化 | 低 | Langfuse UI 树形结构 |
| **P3** | GENERATION Output 是合成摘要 | 高（与 P1 同根因） | LLM 输出审查、prompt 调优 |
| **P4** | 重复 Span | 低（P1 修复后消失） | UI 整洁度 |
| **P5** | 工具调用错误未标记 level | 低 | 告警、错误追踪 |
| **P6** | final_answer 无 input | 低 | 完整性 |

**建议修复顺序**: P0 → P1+P3（同根因一起修） → P2 → P5 → P4+P6（自然消失或低优先级）
