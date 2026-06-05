# E2E-05 搜索技能回归测试报告

**测试时间**: 2026-05-01 23:16 CST (UTC 15:16)
**测试环境**: xiaopaw-v2 + Sandbox MCP + Langfuse
**测试文件**: `tests/e2e/test_e2e_05_search.py`
**Trace ID**: `54bae50cfe7f4771`
**Session ID**: `s-752a5ba29c7f`

## 1. 测试结果

| 项目 | 结果 |
|------|------|
| 测试状态 | ✅ PASSED |
| 总耗时 | 185.46s (3分05秒) |
| LLM-as-Judge | ✅ 通过（Qwen3-max 判定回复与 Python 3.13 相关且含具体信息） |
| Langfuse Trace | ✅ 21 个 observation 全部记录 |
| 搜索结果质量 | ✅ 包含 GIL/Nogil、错误消息改进、typing 增强等具体特性 |

## 2. 完整调用链（按时间顺序）

```
用户输入: "帮我搜索一下 Python 3.13 有什么新特性"
routing_key: p2p:ou_search

┌─────────────────────────────────────────────────────────────────────┐
│ Layer 1: Runner + Session Manager                                   │
│ session-s-752a5ba29c7f (root span, 181.0s)                         │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │ Layer 2: Main Crew (agent_execution, 179.2s)                 │   │
│  │                                                              │   │
│  │  ① llm-call-1 [GENERATION] qwen3-max  (4.6s)               │   │
│  │     → 识别意图: 需要搜索能力                                │   │
│  │     → 决策: 调用 skill_loader(baidu_search)                 │   │
│  │                                                              │   │
│  │  ┌───────────────────────────────────────────────────────┐   │   │
│  │  │ Layer 3: Sub-Crew via SkillLoaderTool (174.8s)        │   │   │
│  │  │ skill_name=baidu_search                               │   │   │
│  │  │                                                       │   │   │
│  │  │  ② llm-call-2 [GENERATION] qwen3-max  (5.6s)        │   │   │
│  │  │     → 决策: 先创建输出目录                            │   │   │
│  │  │                                                       │   │   │
│  │  │  ③ tool: sandbox_execute_bash  (0.8s)                │   │   │
│  │  │     cmd: mkdir -p /workspace/.../outputs              │   │   │
│  │  │                                                       │   │   │
│  │  │  ④ llm-call-3 [GENERATION] qwen3-max  (6.7s)        │   │   │
│  │  │     → 决策: 执行百度搜索脚本                          │   │   │
│  │  │                                                       │   │   │
│  │  │  ⑤ tool: sandbox_execute_bash  (2.9s)                │   │   │
│  │  │     cmd: cd /mnt/skills/baidu_search &&               │   │   │
│  │  │          python search.py --query "Python 3.13 新特性" │   │   │
│  │  │          --top_k 20                                    │   │   │
│  │  │                                                       │   │   │
│  │  │  ⑥ llm-call-4 [GENERATION] qwen3-max  (131.2s) ⚠️   │   │   │
│  │  │     → 处理搜索结果（17条）                            │   │   │
│  │  │     → 决策: 保存 JSON 结果到文件                      │   │   │
│  │  │                                                       │   │   │
│  │  │  ⑦ tool: sandbox_file_operations  (0.2s)             │   │   │
│  │  │     action: write search_result.json                  │   │   │
│  │  │                                                       │   │   │
│  │  │  ⑧ llm-call-5 [GENERATION] qwen3-max  (8.2s)        │   │   │
│  │  │     → 决策: 再次搜索并重定向输出                      │   │   │
│  │  │                                                       │   │   │
│  │  │  ⑨ tool: sandbox_execute_bash  (2.9s)                │   │   │
│  │  │     cmd: search.py --query "Python 3.13 新特性"       │   │   │
│  │  │          --top_k 20 > .../search_result_raw.json      │   │   │
│  │  │                                                       │   │   │
│  │  │  ⑩ llm-call-6 [GENERATION] qwen3-max  (6.5s)        │   │   │
│  │  │     → 决策: 读取保存的原始结果                        │   │   │
│  │  │                                                       │   │   │
│  │  │  ⑪ tool: sandbox_file_operations  (0.2s)             │   │   │
│  │  │     action: read search_result_raw.json               │   │   │
│  │  │                                                       │   │   │
│  │  │  ⑫ llm-call-7 [GENERATION] qwen3-max  (5.0s)        │   │   │
│  │  │     → 判定: 搜索任务完成                              │   │   │
│  │  │                                                       │   │   │
│  │  │  ⑬ tool: final_answer  (0.0s)                        │   │   │
│  │  │     → Sub-Crew 返回: "搜索任务成功完成"               │   │   │
│  │  └───────────────────────────────────────────────────────┘   │   │
│  │                                                              │   │
│  │  ⑭ llm-call-1 [GENERATION] qwen3-max  (10.7s)              │   │
│  │     → 综合 Sub-Crew 搜索结果，生成用户友好回复             │   │
│  │                                                              │   │
│  │  ⑮ tool: final_answer  (0.0s)                               │   │
│  │     → Main Crew 返回最终回复                                │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  ⑯ task-complete span                                              │
│  ⑰ session_end span                                                │
└─────────────────────────────────────────────────────────────────────┘
```

## 3. Langfuse Observation 统计

| 类型 | 数量 | 说明 |
|------|------|------|
| SPAN (root) | 1 | session-s-752a5ba29c7f |
| SPAN (tool) | 12 | 工具调用 span（含 agent_execution × 2, skill_loader × 1, sandbox_bash × 4, sandbox_file × 3, final_answer × 2） |
| SPAN (lifecycle) | 2 | task-complete + session_end |
| GENERATION | 8 | LLM 调用（Main Crew 2次 + Sub-Crew 6次） |
| **合计** | **21** | |

## 4. LLM 调用分析

| 调用 | 层级 | 耗时 | 作用 |
|------|------|------|------|
| Main llm-call-1 | Main Crew | 4.6s | 意图识别 → 路由到 baidu_search skill |
| Sub llm-call-2 | Sub-Crew | 5.6s | 规划执行：先创建输出目录 |
| Sub llm-call-3 | Sub-Crew | 6.7s | 执行搜索脚本（--top_k 20） |
| Sub llm-call-4 | Sub-Crew | **131.2s** ⚠️ | 处理17条搜索结果 + 保存JSON |
| Sub llm-call-5 | Sub-Crew | 8.2s | 二次搜索（带重定向） |
| Sub llm-call-6 | Sub-Crew | 6.5s | 读取原始结果文件 |
| Sub llm-call-7 | Sub-Crew | 5.0s | 完成判定 + 产出最终答案 |
| Main llm-call-1 | Main Crew | 10.7s | 综合结果 → 生成用户回复 |
| **总 LLM 时间** | | **178.5s** | 占总耗时 98.6% |

## 5. 关键发现

### ✅ 正常行为
1. **两层架构工作正常**: Main Crew → SkillLoaderTool → Sub-Crew → Sandbox MCP 全链路打通
2. **搜索结果准确**: 返回 Python 3.13 的具体特性（Nogil/GIL移除、错误消息改进、typing增强）
3. **Langfuse 追踪完整**: 21个 observation 覆盖所有事件点，span 层级清晰
4. **LLM-as-Judge 通过**: Qwen3-max 判定回复满足语义相关性标准

### ⚠️ 性能瓶颈
1. **llm-call-4 耗时 131.2s**: 占总耗时 72%。原因是 Sub-Crew 在处理 17 条搜索结果时，Qwen3-max 的输入 token 量大（搜索结果全文）+ 输出 JSON 写入。这是整个流程的主要瓶颈。
2. **Sub-Crew 执行了两次搜索**: 第一次正常搜索后，又执行了一次带重定向的搜索（保存 raw JSON），可能是 Skill 指令中的流程要求，但增加了约 11s 延迟。
3. **8 次 LLM 调用**: 总 LLM 时间 178.5s，占总耗时 98.6%。工具执行本身极快（sandbox bash < 3s, file ops < 0.2s）。

### 🔍 Langfuse Trace 质量
- **source 标识**: `xiaopaw-v2`（正确）
- **span 生命周期**: 部分 span 通过 `auto-closed-by-next-llm` 和 `auto-closed-by-after-turn` 机制自动关闭，符合设计（Hook 机制）
- **完成 span**: `task-complete` 和 `session_end` 正确触发
- **Parent 层级**: root span → tool span → generation span 层级清晰

## 6. 功能覆盖对应

| 功能域 | 覆盖内容 | E2E-05 验证点 |
|------|---------|---------------|
| Function Calling | Agent 通过 LLM 决策调用工具 | ✅ llm-call-1 → skill_loader |
| BaseTool args_schema | SkillLoaderTool 参数定义 | ✅ skill_name + task_context |
| SkillLoaderTool | Progressive disclosure → Sub-Crew | ✅ 加载 SKILL.md → 创建 Sub-Crew |
| 两层架构 | Main → Sub 双 Crew 协作 | ✅ Main Crew → Sub-Crew → 结果回传 |
| Hook 事件体系 | 5+2 事件 → Langfuse trace | ✅ 21 个 observation 完整记录 |

## 7. 结论

E2E-05 搜索技能端到端测试 **全部通过**。两层架构（Main Crew → SkillLoaderTool → Sub-Crew → Sandbox MCP）工作正常，Langfuse 全链路追踪完整。

主要优化方向：llm-call-4 的 131s 耗时是性能瓶颈，可考虑：
- 限制传入 LLM 的搜索结果数量（当前 --top_k 20 → 可降至 10）
- 搜索结果预处理/截断，减少 LLM 输入 token
- Sub-Crew 搜索流程精简（避免二次搜索）
