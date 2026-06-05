# 12. Hook 框架加固——可观测性 + 可靠性 + 安全（模块五集成）

- **版本**：v3.1-rc1（2026-04-24，含 4 路 Review + E2E 反向验证修订）
- **前置版本**：DESIGN.md v2.1（2026-04-19）
- **设计范围**：可观测性（L30）、可靠性（L31）、安全（L32）→ **整合到 XiaoPaw（L33）**
- **设计原则**：保留 v2 全部设计意图和已有加固，在此基础上叠加 Hook 框架的三层运行时保护

---

## 目录

1. [概述与动机](#1-概述与动机)
2. [Hook 框架架构](#2-hook-框架架构)
3. [可观测性升级（30 课）](#3-可观测性升级30-课)
4. [可靠性策略（31 课）](#4-可靠性策略31-课)
5. [安全策略（32 课）](#5-安全策略32-课)
6. [与 v2 现有模块的集成](#6-与-v2-现有模块的集成)
7. [目录结构变更](#7-目录结构变更)
8. [新增 Feature Flags](#8-新增-feature-flags)
9. [威胁模型更新](#9-威胁模型更新)
10. [测试策略补充](#10-测试策略补充)
11. [v2 → v3 迁移](#11-v2--v3-迁移)
12. [4 路 Review 发现与设计修订（ADR-v3）](#12-4-路-review-发现与设计修订adr-v3)
13. [E2E 反向验证——设计缺口修订（ADR-v3-E2E）](#13-e2e-反向验证用集成测试用例审视设计缺口adr-v3-e2e)

---

## 1. 概述与动机

### 1.1 v2 的缺口

XiaoPaw v2 在基础设施层做了大量加固（并发锁、凭证校验、容器安全、Prometheus 指标），但在 **Agent 运行时层** 存在三个结构性盲区：

| 盲区 | 现状（v2） | 目标（v3） |
|------|-----------|-----------|
| **Agent 行为不可见** | trace_id 贯穿请求链，但 Agent 内部的推理步骤、工具调用、token 消耗只有 Prometheus 总量计数 | 5+2 事件体系 + Langfuse Trace 树，精确到每一步推理和工具调用 |
| **Agent 行为不可控** | `max_iter` 硬截断是唯一的循环防线；无成本围栏；重试靠 tenacity 但无状态追踪 | 状态哈希循环检测 + 实时成本围栏 + 重试可观测——Hook 层自动干预 |
| **Agent 工具不受限** | MCP 白名单管 Sub-Crew 工具暴露；memory-save 有 BLOCKED_PATTERNS——但 Main Agent 的工具调用无运行时输入检查 | 沙箱输入消毒 + 权限网关（Deny > Ask > Allow）+ 密钥工具层注入——Hook 层确定性拦截 |

### 1.2 设计思路

**一个骨架，三层插件。**

30 课搭建的 Hook 框架（HookRegistry + HookLoader + CrewObservabilityAdapter）是统一骨架。31 课的可靠性策略、32 课的安全策略、以及现有的观测 handler，全部作为插件注册在同一个骨架上。改 Hook 不改代码，改 YAML 就行。

```
v2 已有层                    v3 新增层
─────────────                ─────────────
基础设施安全                  Agent 运行时保护
(凭证/容器/网络)              (Hook 框架)
        │                          │
        ├── Prometheus 指标        ├── 5+2 事件体系
        ├── trace_id ContextVar    ├── Langfuse Trace 树
        ├── PII 脱敏              ├── 结构化事件日志
        ├── MCP 白名单            ├── SandboxGuard 输入消毒
        ├── memory 投毒过滤        ├── PermissionGate 权限网关
        ├── 凭证安全校验           ├── SecureToolWrapper 密钥注入
        ├── 入站速率限制           ├── LoopDetector 循环检测
        └── filelock 并发保护      ├── CostGuard 成本围栏
                                   └── RetryTracker 重试追踪
```

**v2 层和 v3 层是叠加关系，不是替换。** v2 的 Prometheus 指标、trace_id、PII 脱敏等全部保留。v3 的 Hook 框架在 v2 之上增加 Agent 运行时的细粒度保护。

---

## 2. Hook 框架架构

### 2.1 5+2 事件体系

所有 Agent 框架的执行单元都是 Turn——Agent 想一步（LLM 推理）、做一步（工具调用）。事件类型对齐 Turn 周期：

```
Agent Turn 周期（5 个事件）：
BEFORE_TURN → BEFORE_LLM → [LLM推理] → BEFORE_TOOL_CALL → [工具执行] → AFTER_TOOL_CALL → AFTER_TURN
                                         （无工具调用时直接 → AFTER_TURN）

生命周期事件（2 个）：
TASK_COMPLETE（任务完成）   SESSION_END（会话结束）
```

```python
class EventType(Enum):
    BEFORE_TURN = "before_turn"
    BEFORE_LLM = "before_llm"
    BEFORE_TOOL_CALL = "before_tool_call"
    AFTER_TOOL_CALL = "after_tool_call"
    AFTER_TURN = "after_turn"
    TASK_COMPLETE = "task_complete"
    SESSION_END = "session_end"
```

### 2.2 HookContext（frozen dataclass）

```python
@dataclass(frozen=True)
class HookContext:
    event_type: EventType
    timestamp: str
    agent_id: str = ""
    tool_name: str = ""
    tool_input: dict = field(default_factory=dict)
    session_id: str = ""         # 映射 v2 的 routing_key
    sender_id: str = ""          # 消息发送者（"cron" / "ou_xxx"），见 ADR-v3-E2E-009
    turn_number: int = 0
    input_tokens: int = 0        # 粗估 token（成本围栏用）
    output_tokens: int = 0
    success: bool = True
    metadata: dict = field(default_factory=dict)
```

**frozen=True** 保证 handler 只能读取上下文，不能修改——防止一个 handler 影响后续 handler。

### 2.3 HookRegistry：dispatch + dispatch_gate

```python
class GuardrailDeny(Exception):
    """可靠性/安全策略拒绝当前操作时抛出。"""

class HookRegistry:
    def dispatch(self, event_type, context):
        """报警器：handler 异常被吞掉，不影响执行。"""
        for handler in self._handlers[event_type]:
            try:
                handler(context)
            except Exception as e:
                print(f"handler error: {e}", file=sys.stderr)

    def dispatch_gate(self, event_type, context):
        """保险丝：GuardrailDeny 传播，其他异常吞掉。"""
        for handler in self._handlers[event_type]:
            try:
                handler(context)
            except GuardrailDeny:
                raise    # 策略拒绝 → 传播
            except Exception as e:
                print(f"handler error: {e}", file=sys.stderr)
```

**关键设计决策**：
- `dispatch` 用于观测类 handler（Langfuse 网络超时不应停 Agent）
- `dispatch_gate` 用于策略类 handler（循环检测/成本围栏需要阻断能力）
- 实例级注册（每个 Crew 实例有自己的 HookRegistry），解决 v2 `@before_llm_call` 全局污染问题

### 2.4 HookLoader：YAML 两层配置 + 策略加载

```yaml
# shared_hooks/hooks.yaml（实际 72 行）

# 观测层（dispatch，fire-and-forget）
# BEFORE_TOOL_CALL 的观测 handler 在 dispatch_gate 中先于策略 handler 执行，
# 确保即使被 deny 也能在 Langfuse 中留下记录。
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
    - handler: structured_log.after_turn_handler
    - handler: langfuse_trace.after_turn_handler
  TASK_COMPLETE:
    - handler: structured_log.task_complete_handler
    - handler: langfuse_trace.task_complete_handler
  SESSION_END:
    - handler: structured_log.session_end_handler
    - handler: langfuse_trace.flush_and_close

# 策略层（dispatch_gate，可阻断）
strategies:
  # 安全策略（先于可靠性）
  - name: audit_logger
    class: audit_logger.SecurityAuditLogger
    config: {}
    hooks:
      SESSION_END: session_end_handler

  - name: sandbox_guard
    class: sandbox_guard.SandboxGuard
    config: {}
    deps:
      audit: audit_logger
    hooks:
      BEFORE_TOOL_CALL: before_tool_handler

  - name: permission_gate
    class: permission_gate.PermissionGate
    config: {}
    deps:
      audit: audit_logger
    hooks:
      BEFORE_TOOL_CALL: before_tool_handler

  # 可靠性策略（后于安全）
  - name: cost_guard
    class: cost_guard.CostGuard
    config:
      budget_usd: 1.0
    hooks:
      AFTER_TURN: after_turn_handler
      BEFORE_TOOL_CALL: before_tool_handler

  - name: loop_detector
    class: loop_detector.LoopDetector
    config:
      threshold: 3
    hooks:
      AFTER_TOOL_CALL: after_tool_handler
      AFTER_TURN: after_turn_handler

  - name: retry_tracker
    class: retry_tracker.RetryTracker
    config:
      max_retries: 5
    hooks:
      AFTER_TOOL_CALL: after_tool_handler
```

**两层加载**：
1. **全局层**（`shared_hooks/hooks.yaml`）：基线观测 + 安全策略 + 可靠性策略，所有 Agent 共享
2. **Workspace 层**（`workspace/{agent}/hooks/hooks.yaml`）：业务定制 handler，仅本 Agent

全局先加载，Workspace 追加。同一事件的 handler 按注册顺序执行。

**deps 依赖注入**：`SandboxGuard` 和 `PermissionGate` 共享同一个 `SecurityAuditLogger` 实例。`deps: { audit: audit_logger }` 表示"把 `audit` 参数设为前面已实例化的 `audit_logger`"。声明顺序 = 依赖顺序。

### 2.5 CrewObservabilityAdapter：CrewAI 适配层

适配层把 CrewAI 的 4 种 Hook 机制映射到 7 种通用事件类型：

| CrewAI 机制 | 映射事件 | 映射逻辑 |
|------------|---------|---------|
| `@before_llm_call` | `BEFORE_TURN`（首次）+ `BEFORE_LLM`（每次） | `_current_turn_has_llm` 标志位区分 |
| `@before_tool_call` | `BEFORE_TOOL_CALL` | 直接映射 |
| `@after_tool_call` | `AFTER_TOOL_CALL` | 直接映射 |
| `step_callback` | `AFTER_TURN` | 重置 `_current_turn_has_llm` |
| `task_callback` | `TASK_COMPLETE` | 直接映射 |
| 手动调用 | `SESSION_END` | cleanup 时触发 |

**pending_deny 机制**：CrewAI 的 `@before_tool_call` 内部吞掉异常。当 `dispatch_gate` 抛出 `GuardrailDeny` 时，适配层 catch 住、存到 `_pending_deny`、返回 `False`（CrewAI 原生阻止工具），下一次 `step_callback` 触发时在安全上下文中重新抛出。

**与 v2 `@before_llm_call` 的共存**：v2 的 MemoryAwareCrew 使用 `@before_llm_call` 做 Bootstrap/prune/compress。Hook 框架的适配层也注册 `@before_llm_call`。CrewAI 的 `@before_llm_call` 是一个全局 handler 列表，两者共存——适配层的 handler 负责 Hook 事件分发，MemoryAwareCrew 的 handler 负责记忆管理，互不干扰。**注册顺序：先注册适配层（先触发 BEFORE_TURN/BEFORE_LLM 事件），再注册记忆管理。**

---

## 3. 可观测性升级（30 课）

### 3.1 v2 → v3 可观测性对比

| 维度 | v2（已有） | v3（新增） | 关系 |
|------|-----------|-----------|------|
| **请求级追踪** | trace_id ContextVar 贯穿入口→LLM→Skill→出站 | 5+2 事件体系 + HookContext，精确到每个 Turn 和工具调用 | 互补：trace_id 跟踪请求级，Hook 事件跟踪步骤级 |
| **指标** | 8 个 Prometheus 核心指标 | 策略度量（retry_success_rate, loop_detections, budget_utilization） | 互补：Prometheus 看趋势，策略度量看单次执行 |
| **日志** | JSON 结构化日志 + PII 脱敏 | Hook 结构化事件日志（JSON to stderr，每事件一行） | 共存：现有日志不变，Hook 事件日志是额外通道 |
| **可视化** | Prometheus/Grafana Dashboard | Langfuse Trace 树 + 成本面板 + Prompt 版本 | 互补：Grafana 看系统级，Langfuse 看 Agent 推理级 |
| **审计** | 无独立审计通道 | SecurityAuditLogger（JSONL 审计日志）| 新增 |

### 3.2 Langfuse 接入

Langfuse 作为 Hook handler 接入，不是独立组件。在 XiaoPaw v3 中：

- **部署**：Docker 自托管（web + worker + postgres + clickhouse + redis + minio），`langfuse-docker-compose.yaml`
- **接入**：通过 `langfuse_trace.py` handler 注册在 `BEFORE_LLM`、`AFTER_TOOL_CALL`、`SESSION_END` 事件上
- **session 映射**：XiaoPaw 的 `routing_key`（如 `p2p:ou_xxx`）映射为 Langfuse 的 `session_id`
- **数据安全**：Langfuse 自托管确保数据不出内网；可配置 `LANGFUSE_MASK_INPUTS=true` 对 prompt 做脱敏
- **采样策略**：错误/高成本请求 100% 保留，成功请求 10% 采样（tail-based sampling）

**环境变量**：
```bash
LANGFUSE_PUBLIC_KEY="pk-lf-xxx"
LANGFUSE_SECRET_KEY="sk-lf-xxx"
LANGFUSE_HOST="http://localhost:3000"
LANGFUSE_ENABLED=true           # feature flag，见 §8
```

### 3.3 结构化事件日志

`structured_log.py` handler 把每个 Hook 事件输出为一行 JSON 到 stderr：

```json
{"timestamp":"...","event":"before_turn","session_id":"p2p:ou_xxx","turn":1,"agent_id":"XiaoPaw Main"}
{"timestamp":"...","event":"before_tool_call","session_id":"p2p:ou_xxx","turn":1,"tool":"skill_loader"}
```

**与 v2 现有日志的关系**：v2 的 `logging_config.py` JSON 日志面向应用层（INFO/WARNING/ERROR 级别），Hook 事件日志面向 Agent 行为层（每个推理步骤和工具调用）。两者独立输出到不同通道，不互相干扰。

---

## 4. 可靠性策略（31 课）

### 4.1 RetryTracker（纯观测）

纯观测，不阻断。追踪每个工具的连续失败次数和重试成功率。

```python
class RetryTracker:
    def after_tool_handler(self, ctx):
        if not ctx.success:
            prev = self._failures.get(ctx.tool_name, 0)
            self._failures[ctx.tool_name] = prev + 1
            if prev > 0:
                self._total_retries += 1
            if self._failures[ctx.tool_name] >= self._max_retries:
                self._emit_warning(ctx.tool_name)
        else:
            if self._failures.get(ctx.tool_name, 0) > 0:
                self._successful_retries += 1
            self._failures[ctx.tool_name] = 0
```

**度量输出**：`retry_success_rate` 低于 10% → 说明在浪费钱重试不可恢复的错误。

**与 v2 tenacity 重试的关系**：v2 的 `utils/retry.py` 用 tenacity 做 LLM/API 层的指数退避重试。RetryTracker 不替代 tenacity，而是在 Hook 层追踪重试效果——tenacity 做重试，RetryTracker 观测重试是否有效。

### 4.2 LoopDetector（活跃护栏）

状态哈希去重。对 `tool_name + output` 做 MD5 哈希，连续 N 次（默认 3）哈希相同即判定循环：

```python
class LoopDetector:
    def _check_loop(self, hashes, state, ctx):
        h = hashlib.md5(state.encode()).hexdigest()[:16]
        hashes.append(h)
        if len(hashes) >= self._threshold:
            recent = hashes[-self._threshold:]
            if len(set(recent)) == 1:
                raise GuardrailDeny("Loop detected: identical state repeated ...")
```

**双路径覆盖**：`_tool_hashes`（AFTER_TOOL_CALL）检测工具调用循环，`_turn_hashes`（AFTER_TURN）检测推理循环。覆盖 CrewAI 的 ReAct 和原生 function calling 两种模式。

**与 v2 `max_iter` 的关系**：`max_iter` 是最后防线（不管内容是否重复），哈希去重是精准检测（第 3 次重复就发现）。双保险。

### 4.3 CostGuard（活跃护栏）

实时 token 成本计算 + 预算硬停止：

```python
MODEL_PRICES = {
    "qwen-plus":  {"input": 0.80, "output": 2.00},
    "qwen-turbo": {"input": 0.30, "output": 0.60},
    "qwen-max":   {"input": 2.40, "output": 9.60},
}

class CostGuard:
    def after_turn_handler(self, ctx):
        self._total_input_tokens += ctx.input_tokens
        self._total_output_tokens += ctx.output_tokens
        self._estimated_cost = self._calculate_cost()
        if self._estimated_cost >= self._budget:
            raise GuardrailDeny(f"Budget exceeded: ${self._estimated_cost:.4f}")

    def before_tool_handler(self, ctx):
        if self._estimated_cost >= self._budget:
            raise GuardrailDeny(...)
```

**双重检查**：推理完查一次（AFTER_TURN），工具调用前再查一次（BEFORE_TOOL_CALL）。

**token 来源**：适配层粗估（`max(1, len(text) * 2 // 3)`）。与 v2 的 `token_counter.py`（Qwen 官方 tokenizer）互补——粗估用于围栏决策（±20% 不影响"预算用完没"的判断），精确计数走 Langfuse 成本面板做事后分析。

### 4.4 BEFORE_TOOL_CALL 执行链

策略注册顺序决定执行顺序。`dispatch_gate` 在第一个 `GuardrailDeny` 处停止后续 handler：

```
BEFORE_TOOL_CALL 执行链（v3 完整）：

  ┌─────────────────────────────────────────────────────┐
  │ dispatch_gate 按注册顺序执行，第一个 Deny 即停止    │
  ├─────────────────────────────────────────────────────┤
  │ 1. sandbox_guard.before_tool_handler  ← 输入消毒   │
  │ 2. permission_gate.before_tool_handler ← 权限检查  │
  │ 3. cost_guard.before_tool_handler     ← 预算检查   │
  └─────────────────────────────────────────────────────┘
```

**AFTER_TURN 执行链**：
```
  ┌───────────────────────────────────────────────────┐
  │ 1. cost_guard.after_turn_handler   ← 先记录成本  │
  │ 2. loop_detector.after_turn_handler ← 再检循环    │
  └───────────────────────────────────────────────────┘
```

成本累加必须在循环检测之前：如果循环检测先 Deny，已发生的成本未被记录。

---

## 5. 安全策略（32 课）

### 5.1 SandboxGuard：确定性输入消毒

在 `BEFORE_TOOL_CALL` 对工具输入做正则检查，零 LLM 依赖：

| 检查项 | 正则 | 行为 |
|--------|------|------|
| 路径遍历 | `\.\.[/\\]` | **阻断** + 记录审计 |
| 危险命令 | `rm -rf`, `sudo`, `chmod 777`, `curl\|sh`, `eval`, `exec`, `dd`, `mkfs`, `shred` | **阻断** + 记录审计 |
| Shell 注入 | `[;&\|` + `` ` `` + `$(` | **阻断** + 记录审计 |
| 环境变量引用 | `\$\{?\w+\}?` | **警告** + 记录日志 |
| Prompt 注入标记 | `\[(SYSTEM\|INST)\]`, `<\|?system\|?>`, `忽略.*指令` | **阻断** + 记录审计（ADR-v3-E2E-003）|

**输入预处理**：`urllib.parse.unquote(raw)` URL 解码，防止 `%2e%2e%2f`（`../`）绕过。

**与 v2 现有安全的关系**：v2 已有路径遍历防护（`Path.resolve().is_relative_to()`）和 memory-save 投毒过滤（`BLOCKED_PATTERNS`）。SandboxGuard 是**前置关卡**——在工具执行前检查输入，v2 的防护是**后置边界**——在特定模块内部校验。前后呼应，纵深防御。

### 5.2 PermissionGate：工具权限网关

Deny > Ask > Allow 三级权限，YAML 配置驱动：

```yaml
# workspace/{agent}/security.yaml
permissions:
  default: ask
  tools:
    skill_loader: allow         # 核心功能
    baidu_search: allow         # 只读搜索
    add_image: ask              # 写文件，需关注
    shell_executor: deny        # 任意命令，禁止
```

**Default-Deny 原则**：`default: ask` 意味着新增工具默认需要确认。

**与 v2 MCP 白名单的关系**：v2 的 MCP 白名单管 Sub-Crew（Skill 级别的工具暴露），PermissionGate 管 Main Agent（Agent 级别的工具使用权限）。两者互补：

```
Main Agent → PermissionGate（v3 新增）→ SkillLoaderTool
                                            │
                                      Sub-Crew (Sandbox)
                                            │
                               MCP 白名单（v2 已有）→ sandbox tools
```

### 5.3 SecurityAuditLogger：JSONL 审计日志

append-only JSONL 审计日志，独立于应用日志：

```json
{"timestamp":"...","security_event":"sandbox_path_traversal","tool":"skill_loader","input_preview":"../../etc/passwd"}
{"timestamp":"...","security_event":"permission_deny","tool":"shell_executor"}
{"timestamp":"...","security_event":"session_summary","total_security_events":2}
```

SESSION_END 时写入汇总记录。**审计文件路径**：`data/security_audit/{routing_key}/audit.jsonl`，按用户隔离。

**与 v2 审计的关系**：v2 有 raw audit log（`data/ctx/{sid}_raw.jsonl`），记录对话历史。SecurityAuditLogger 记录安全事件——被拦截的工具调用、权限检查决策、违规详情。两者目的不同。

### 5.4 SecureToolWrapper：密钥工具层注入

LLM 只看到工具名和描述，永远不接触 API Key：

```python
class SecureToolWrapper:
    @staticmethod
    def wrap(tool, credentials):
        original_run = tool._run
        resolved = {param: os.environ[env_var] for param, env_var in credentials.items()}
        def wrapped_run(**kwargs):
            return original_run(**{**kwargs, **resolved})
        tool._run = wrapped_run
        return tool
```

**与 v2 凭证管理的关系**：v2 的凭证管理（config.yaml + `.env` + safety.py 校验）管的是**存储和启动时校验**。SecureToolWrapper 管的是**运行时注入**——确保 API Key 在工具 `_run()` 执行时从环境变量注入，不经过 LLM 上下文。

---

## 6. 与 v2 现有模块的集成

### 6.1 集成点清单

| v2 模块 | 集成方式 | 详细说明 |
|---------|---------|---------|
| `Runner` | **入口点**：创建 HookRegistry + 加载 hooks + 初始化适配层 | Runner 持有 registry 实例，每个消息处理周期初始化适配层 |
| `MemoryAwareCrew` | **共存**：适配层的 `@before_llm_call` 和记忆管理的 `@before_llm_call` 共存于 CrewAI 全局列表 | 适配层先注册（事件分发），记忆管理后注册（Bootstrap/prune/compress） |
| `SkillLoaderTool` | **被保护**：PermissionGate 检查 Main Agent 的工具使用权限 | `skill_loader: allow` 在 security.yaml 中声明 |
| `observability/trace` | **共存**：trace_id 继续贯穿请求链，Hook 事件日志通过 `metadata.trace_id` 关联 | HookContext.metadata 中携带 v2 trace_id |
| `observability/metrics` | **扩展**：新增 Hook 策略相关 Prometheus 指标 | 见 §6.3 |
| `observability/security` | **增强**：RateLimiter 保持不变，SandboxGuard 增加运行时输入检查 | 入站限流（v2）+ 工具输入消毒（v3） |
| `config/flags` | **扩展**：新增 6 个 feature flag | 见 §8 |
| `feishu/sender` | **不变** | Hook 框架不涉及出站消息 |
| `cron/service` | **被保护**：Cron 触发的任务执行也经过 Hook 框架 | CronService dispatch 消息时走 Runner，自动受 Hook 保护 |
| `workspace/` | **隔离**：per-routing_key 目录隔离（ADR-v3-E2E-004） | `data/workspace/{safe_routing_key}/` 替代全局 `data/workspace/` |

### 6.2 Runner 集成伪代码

```python
# xiaopaw/runner.py（v3 变更）
async def _handle(self, msg: InboundMessage):
    # v2 已有逻辑
    session = await self._session_mgr.load_or_create(msg.routing_key)

    # v3 新增：Hook 框架初始化
    registry = HookRegistry()
    loader = HookLoader(registry)
    loader.load_two_layers(
        global_dir=SHARED_HOOKS_DIR,
        workspace_dir=session.workspace_dir,
    )
    adapter = CrewObservabilityAdapter(
        registry,
        session_id=msg.routing_key,
    )
    adapter.install_global_hooks()

    try:
        # v2 已有逻辑（MemoryAwareCrew 构建）
        crew = self._build_crew(
            session,
            step_callback=adapter.make_step_callback(),
            task_callback=adapter.make_task_callback(),
        )
        result = await asyncio.to_thread(crew.kickoff)
    except GuardrailDeny as e:
        # v3 新增：策略拒绝 → 安全终止 + 用户提示
        await self._sender.send_text(msg.routing_key, f"⚠️ 安全策略终止：{e.reason}")
    finally:
        adapter.cleanup()
```

### 6.3 新增 Prometheus 指标

在 v2 的 8 个核心指标基础上新增：

| 指标 | 类型 | 标签 | 说明 |
|------|------|------|------|
| `xiaopaw_hook_guardrail_deny_total` | Counter | `strategy`, `reason` | 策略拒绝次数（循环/成本/安全） |
| `xiaopaw_hook_loop_detected_total` | Counter | `detection_type` | 循环检测命中（tool_loop / turn_loop） |
| `xiaopaw_hook_cost_usd` | Gauge | `routing_key` | 当前会话累计成本（美元） |
| `xiaopaw_hook_security_violations_total` | Counter | `violation_type` | 安全违规次数（path_traversal / dangerous_command / shell_injection） |

### 6.4 trace_id 关联

v2 的 `trace_id`（ContextVar）和 v3 的 `session_id`（HookContext 字段）通过以下方式关联：

```python
# Runner._handle 中
from xiaopaw.observability.trace import get_trace_id

ctx = HookContext(
    event_type=event_type,
    session_id=msg.routing_key,
    metadata={"trace_id": get_trace_id()},  # v2 trace_id 注入 Hook 事件
)
```

这样 Langfuse Trace 树和 Prometheus 指标可以通过 `trace_id` 关联到同一个请求。

---

## 7. 目录结构变更

```diff
 xiaopaw-v2/
 ├── DESIGN.md
 ├── docs/
 │   ├── 01-architecture.md ~ 11-migration.md
+│   ├── 12-hook-hardening.md          # 本文档
 │   └── ssot/
+│       ├── hook-events.md            # 7 种事件类型 × 触发条件 × handler 列表
+│       └── strategies.md             # 所有策略 × 配置 × 阈值 × 度量
 │
 ├── xiaopaw/
+│   ├── hook_framework/               # 【v3 新增】Hook 骨架
+│   │   ├── __init__.py
+│   │   ├── registry.py               # EventType + HookContext + HookRegistry + GuardrailDeny
+│   │   ├── loader.py                 # HookLoader（YAML + importlib + deps）
+│   │   └── crew_adapter.py           # CrewObservabilityAdapter
 │   │
 │   ├── agents/
 │   │   ├── main_crew.py              # 不变（记忆管理 @before_llm_call 保留）
 │   │   └── ...
 │   │
 │   ├── observability/
 │   │   ├── logging_config.py         # 不变
 │   │   ├── trace.py                  # 不变
 │   │   ├── pii_mask.py               # 不变
 │   │   ├── metrics.py                # 扩展：新增 4 个 Hook 指标
 │   │   └── ...
 │   │
 │   ├── runner.py                     # 变更：集成 Hook 框架初始化
 │   └── ...
 │
+├── shared_hooks/                     # 【v3 新增】加固层（1337 行，零业务代码修改）
+│   ├── hooks.yaml                    # 两段式配置入口（72 行）
+│   ├── structured_log.py             # JSON 结构化事件日志（82 行）
+│   ├── langfuse_trace.py             # Langfuse trace/span/generation 全链路（779 行）
+│   ├── audit_logger.py               # JSONL 安全审计日志（63 行）
+│   ├── sandbox_guard.py              # 输入消毒：路径穿越/shell/prompt 注入（107 行）
+│   ├── permission_gate.py            # 工具权限 deny/warn/allow（75 行）
+│   ├── cost_guard.py                 # 成本围栏 $1 预算（69 行）
+│   ├── loop_detector.py              # 循环检测阈值 3（50 行）
+│   └── retry_tracker.py              # 重试追踪最大 5 次（40 行）
 │
 ├── workspace-init/                   # 模板目录
 │
 └── tests/                            # 293 用例
+    ├── unit/hook_framework/           # Hook 框架单元测试（64 用例）
+    │   ├── test_hook_registry.py      #   14 用例
+    │   ├── test_hook_loader.py        #   17 用例
+    │   ├── test_crew_adapter.py       #   16 用例
+    │   └── test_edge_cases.py         #   17 用例
+    ├── unit/shared_hooks/             # 加固策略单元测试（106 用例）
+    │   ├── test_sandbox_guard.py      #   27 用例
+    │   ├── test_permission_gate.py    #   10 用例
+    │   ├── test_cost_guard.py         #   11 用例
+    │   ├── test_loop_detector.py      #   8 用例
+    │   ├── test_retry_tracker.py      #   6 用例
+    │   ├── test_audit_logger.py       #   7 用例
+    │   ├── test_structured_log.py     #   6 用例
+    │   ├── test_langfuse_autoclose.py #   29 用例
+    │   └── test_langfuse_init.py      #   2 用例
+    ├── unit/test_v3_fixes.py          # v3 修复验证（18 用例）
+    ├── integration/                   # 集成测试（40 用例）
+    │   ├── test_hook_chain.py         #   8 用例
+    │   ├── test_security_chain.py     #   7 用例
+    │   ├── test_adapter_integration.py #  6 用例
+    │   ├── test_two_layer_config.py   #   4 用例
+    │   ├── test_guardrail_deny_flow.py #  3 用例
+    │   ├── test_trace_quality.py      #   6 用例
+    │   └── test_deny_observability.py #   6 用例
+    └── e2e/                           # E2E 测试（65 用例 / 15 场景 + 2 persona）
+        ├── test_e2e_01~09.py          #   基础功能（slash/routing/multi-turn/bootstrap/search/browse/memory/pruning）
+        ├── test_e2e_10_langfuse_trace.py  # Langfuse trace 验证
+        ├── test_e2e_11_events.py      #   7 种事件验证
+        ├── test_e2e_12_reliability.py #   可靠性策略验证
+        ├── test_e2e_13_sandbox_guard.py # 安全策略验证
+        ├── test_e2e_14_credential_isolation.py # 凭证隔离验证
+        └── test_e2e_15_audit_deny.py  #   审计+拦截验证
```

---

## 8. 新增 Feature Flags

在 v2 的 12 个 flag（F1-F12）基础上新增 6 个：

| Flag | 名称 | 默认值 | 说明 | 回滚影响 |
|------|------|--------|------|---------|
| **F13** | `enable_hook_framework` | `true` | 总开关：关闭后 Runner 跳过 Hook 初始化，回退 v2 行为 | 安全：所有 Hook 保护失效 |
| **F14** | `enable_langfuse` | `true` | Langfuse Trace 上报。关闭后 langfuse_trace handler 不加载 | 安全：仅失去可视化，不影响保护 |
| **F15** | `enable_sandbox_guard` | `true` | 沙箱输入消毒。关闭后跳过正则检查 | 危险：工具输入不再被检查 |
| **F16** | `enable_permission_gate` | `true` | 权限网关。关闭后所有工具默认 allow | 危险：工具无权限控制 |
| **F17** | `enable_cost_guard` | `true` | 成本围栏。关闭后无预算硬停止 | 中等：可能超支但不损坏 |
| **F18** | `enable_loop_detector` | `true` | 循环检测。关闭后仅靠 max_iter | 中等：循环检测退化为纯暴力截断 |

**生产断言**：`assert_all_production_safe()` 扩展为检查 F13-F16 必须为 true。F17/F18 允许关闭但记录 WARNING。

---

## 9. 威胁模型更新

在 v2 的 T1-T11 基础上，v3 的 Hook 框架为多个威胁提供了**运行时防御层**：

| 威胁 | v2 防御 | v3 新增防御 | 残余风险变化 |
|------|--------|-----------|-------------|
| **T1** Prompt Injection → 工具滥用 | MCP 白名单 + sandbox seccomp | SandboxGuard 正则消毒 + PermissionGate 工具权限 | HIGH → **MEDIUM**（双层拦截） |
| **T2** Memory Poisoning | BLOCKED_PATTERNS | SandboxGuard 对 memory-save 输入做前置检查 | MEDIUM-HIGH → **MEDIUM** |
| **T5** 路径遍历 | `Path.resolve()` 边界检查 | SandboxGuard `../` 正则 + URL decode | LOW（进一步加固） |
| **T8** Cron→Runner 注入 | BLOCKED_PATTERNS + Pydantic | CostGuard 限制 Cron 任务的成本上限 | MEDIUM → **LOW** |

新增威胁：

| 威胁 | 描述 | 防御 | 残余风险 |
|------|------|------|---------|
| **T12** Hook 配置篡改 | 攻击者通过 Prompt 注入修改 hooks.yaml 或 security.yaml | 配置文件只读（容器 `read_only: true`）+ SandboxGuard 阻断写配置路径 | LOW |
| **T13** 成本耗尽攻击 | 恶意用户通过复杂任务快速消耗 API 预算 | CostGuard per-session 预算 + per-user 入站速率限制（v2 已有） | LOW |
| **T14** 策略绕过 | 攻击者构造输入绕过正则检查 | 四条正则互补 + URL decode 预处理；但正则非万能，残余风险中等 | MEDIUM |

---

## 10. 测试策略补充

### 10.1 新增测试用例

| 文件 | 测试内容 | 数量 | 类型 |
|------|---------|------|------|
| `test_hook_registry.py` | 注册/分发/dispatch_gate/GuardrailDeny/多handler/异常隔离/summary | 14 | 单元 |
| `test_hook_loader.py` | YAML 加载/两层合并/策略实例化/deps 注入/缺文件/模块不存在 | 17 | 单元 |
| `test_crew_adapter.py` | BEFORE_TURN 计数/step→AFTER_TURN/cleanup/pending_deny/tool 事件 | 16 | 单元 |
| `test_edge_cases.py` | HookContext 不可变/并发安全/正则边界/hash 边界 | 17 | 单元 |
| `test_sandbox_guard.py` | 路径遍历/危险命令/Shell注入/URL编码绕过/环境变量/prompt注入/false positive | 27 | 单元 |
| `test_permission_gate.py` | Deny 阻断/Ask 记录/Allow 放行/default 行为/YAML 加载/大小写 | 10 | 单元 |
| `test_cost_guard.py` | 预算超限/双重检查/模型价格/度量输出/边界条件/env var override | 11 | 单元 |
| `test_loop_detector.py` | 工具循环/推理循环/阈值边界/不同输出不触发/双路径独立 | 8 | 单元 |
| `test_retry_tracker.py` | 连续失败/重试成功/计数重置/度量准确性/空 tool_name | 6 | 单元 |
| `test_audit_logger.py` | 事件累积/JSONL 写入/会话摘要/env var 配置 | 7 | 单元 |
| `test_structured_log.py` | 7 种事件 handler 输出格式验证 | 6 | 单元 |
| `test_langfuse_autoclose.py` | span 自动关闭/batch flush/异常恢复/并发安全 | 29 | 单元 |
| `test_langfuse_init.py` | Langfuse 初始化/env var 缺失降级 | 2 | 单元 |
| `test_v3_fixes.py` | v3 修复回归验证（pending_deny/handler 异常隔离等） | 18 | 单元 |
| `test_hook_chain.py` | 全链路：7 种事件 × 2 层 hook 组合触发 | 8 | 集成 |
| `test_security_chain.py` | 注入攻击 → SandboxGuard → 审计日志 → 策略度量 | 7 | 集成 |
| `test_adapter_integration.py` | Adapter + Registry + 策略链端到端 | 6 | 集成 |
| `test_two_layer_config.py` | 全局 + workspace 两层 YAML 合并 | 4 | 集成 |
| `test_guardrail_deny_flow.py` | Deny 传播 + pending_deny 流转 | 3 | 集成 |
| `test_trace_quality.py` | Langfuse trace 质量验证 | 6 | 集成 |
| `test_deny_observability.py` | Deny 事件可观测性 | 6 | 集成 |
| E2E 测试（15 场景 + 2 persona） | 覆盖全功能知识点 | 65 | E2E |

**实际用例合计**：293 个（单元 188 + 集成 40 + E2E 65）。

### 10.2 覆盖率要求

| 模块 | 目标覆盖率 |
|------|-----------|
| `hook_framework/registry.py` | ≥95% |
| `hook_framework/loader.py` | ≥90% |
| `hook_framework/crew_adapter.py` | ≥90% |
| `shared_hooks/sandbox_guard.py` | ≥95%（安全关键） |
| `shared_hooks/permission_gate.py` | ≥95%（安全关键） |
| `shared_hooks/loop_detector.py` | ≥90% |
| `shared_hooks/cost_guard.py` | ≥90% |
| 其他 handler | ≥85% |

### 10.3 新增 CI Gate

在 v2 的 14 个 CI gate 基础上新增：

- `hook_registry_test_pass`：Hook 框架单元测试全部通过
- `security_strategy_test_pass`：安全策略测试全部通过
- `hook_e2e_test_pass`：Hook 集成测试全部通过
- `sandbox_guard_coverage >= 95`：安全关键模块覆盖率强制

---

## 11. v2 → v3 迁移

### 11.1 变更影响分析

| 影响范围 | 说明 |
|---------|------|
| **新增代码** | `hook_framework/`（4 文件，592 行）+ `shared_hooks/`（9 文件，1337 行）+ 测试（38 文件，293 用例）|
| **变更代码** | `runner.py`（Hook 初始化）+ `observability/metrics.py`（新指标）+ `config/flags.py`（新 flag）|
| **配置变更** | 新增 `shared_hooks/hooks.yaml` + workspace `security.yaml` + `.env` 新增 Langfuse 变量 |
| **部署变更** | 新增 `langfuse-docker-compose.yaml`（可选）|
| **数据格式** | 新增 `data/security_audit/` 目录；不影响现有数据格式 |

### 11.2 迁移步骤

1. **代码迁移**：复制 `hook_framework/` 和 `shared_hooks/` 到 xiaopaw-v2
2. **Runner 改造**：在 `_handle` 方法中集成 Hook 框架初始化（见 §6.2）
3. **配置迁移**：创建 `shared_hooks/hooks.yaml`，配置策略参数
4. **安全配置**：为每个 workspace 创建 `security.yaml`，声明工具权限
5. **Feature Flag**：在 `config.yaml` 中添加 F13-F18，`config/flags.py` 中注册
6. **Langfuse 部署**（可选）：启动 Langfuse Docker，配置环境变量
7. **测试**：运行新增测试用例，验证覆盖率
8. **验收**：72h canary 验证——观察 Hook 事件日志、策略度量、安全审计

### 11.3 回滚方案

`F13 enable_hook_framework = false` 一键关闭 Hook 框架，回退到 v2 行为。所有 Hook handler 不加载，Runner 跳过 Hook 初始化。现有 v2 功能不受影响。

---

## 验收标准（v3 G8-G12）

在 v2 的 G1-G7 基础上新增：

- **G8** Hook 框架集成：7 种事件类型全部在 XiaoPaw 运行时触发且可观测
- **G9** 可靠性验证：循环检测在连续 3 次重复时阻断；成本围栏在超预算时阻断；RetryTracker 度量可查询
- **G10** 安全验证：路径遍历、危险命令、Shell 注入在 SandboxGuard 被拦截；权限 Deny 的工具被阻止
- **G11** Langfuse 可视化：完整 Trace 树在 Dashboard 可查看，包含推理步骤和工具调用
- **G12** 测试覆盖：293 测试用例（188 单元 + 40 集成 + 65 E2E）；安全关键模块覆盖率 ≥95%

---

## 文档交叉索引更新

```
DESIGN.md（总纲）
 ├── 01~11（v2 已有）
 ├── 12-hook-hardening.md（本文档，v3 新增）
 │   ├── Hook 框架架构
 │   ├── 可观测性升级
 │   ├── 可靠性策略
 │   ├── 安全策略
 │   ├── 集成 / 迁移
 │   └── 4 路 Review 修订
 ├── 13-test-design-hook-hardening.md（v3 新增，测试设计）
 └── ssot/
     ├── hook-events.md（v3 新增）
     └── strategies.md（v3 新增）
```

---

## 12. 4 路 Review 发现与设计修订（ADR-v3）

> **Review 日期**：2026-04-24
> **Review 角色**：Architecture Agent / Security Agent / Code-Review Agent / TDD Agent
> **输入**：本文档 v3.0-draft + v2 DESIGN.md + m5l30-32 参考实现 + v1 xiaopaw-with-memory 实际代码

### 12.1 CRITICAL 问题（必须在实现前解决）

#### ADR-v3-001：CrewAI 全局 Hook 在并发 Session 下互踩

**问题**：`@before_llm_call`、`@before_tool_call`、`@after_tool_call` 注册在 CrewAI 的**进程级全局列表**上。v2 的 Runner 为不同 `routing_key` 并行处理消息。当 Session A 和 Session B 同时运行时：
- Session B 的 `install_global_hooks()` 追加到同一全局列表
- Session A 的 `cleanup()` 调用 `clear_before_llm_call_hooks()` 清空**所有** hooks，包括 Session B 的

**设计修订**：
- **方案 A（推荐）**：将 Crew 执行序列化到单个 worker 线程，每次只有一个 Crew 使用全局 hooks。`install_global_hooks()` 前加锁，`cleanup()` 后释放。代价：不同 routing_key 无法并行执行 Crew（但可以并行做 session 加载等 IO 操作）。
- **方案 B**：在 `install_global_hooks()` 中不清除旧 hooks，而是使用**标记机制**——每个 handler 携带 `session_id`，dispatch 时只执行匹配当前 session 的 handler。`cleanup()` 只移除自己的 handler。
- **决策**：采用方案 A。理由：CrewAI 的 `@before_llm_call` handler 不接受 filter 参数，方案 B 需要 monkey-patch CrewAI 内部实现。方案 A 简单可靠，且 XiaoPaw 是单节点单进程部署，Crew 执行本身是 CPU/IO 密集的 LLM 调用，序列化的性能影响有限。
- **实现要点**：Runner 新增 `_crew_exec_lock = asyncio.Lock()`，在 `asyncio.to_thread(crew.kickoff)` 前后持锁。

#### ADR-v3-002：`pending_deny` 可能被静默丢弃

**问题**：当 `dispatch_gate` 在 `BEFORE_TOOL_CALL` 中抛出 `GuardrailDeny` 后，deny 存入 `_pending_deny`，等待 `step_callback` 重新抛出。但如果 CrewAI 在抛出前就因 `max_iter` 或 `AgentFinish` 结束，`step_callback` 不再触发，deny 被静默丢弃。

**设计修订**：在 `cleanup()` 和 `make_task_callback()` 中检查 `_pending_deny`：
```python
def cleanup(self):
    pending = self._pending_deny
    self._pending_deny = None
    if self._cleaned: return
    self._cleaned = True
    self._registry.dispatch(EventType.SESSION_END, ...)
    clear_hooks()
    if pending:
        raise pending  # 确保 deny 不被吞掉

def make_task_callback(self):
    def callback(task_output):
        self._registry.dispatch(EventType.TASK_COMPLETE, ...)
        if self._pending_deny:
            pending = self._pending_deny
            self._pending_deny = None
            raise pending
    return callback
```

### 12.2 HIGH 问题（应在实现中解决）

#### ADR-v3-003：Runner 集成点需从 `_handle` 移到 `agent_fn` 闭包

**问题**（Code-Review Agent）：v1 的 Runner 不直接构建 Crew——它委托给 `build_agent_fn()` 生成的 `agent_fn` 闭包。Runner 从未接触 Crew 对象。§6.2 的伪代码 `self._build_crew(session, step_callback=...)` 与实际架构不符。

**设计修订**：Hook 初始化应发生在 `build_agent_fn()` 内部：
```python
# xiaopaw/agents/build.py（v3 变更）
def build_agent_fn(config, ...):
    def agent_fn(msg, session, sender):
        registry = HookRegistry()
        loader = HookLoader(registry)
        loader.load_two_layers(global_dir=..., workspace_dir=session.workspace_dir)
        adapter = CrewObservabilityAdapter(registry, session_id=msg.routing_key)
        adapter.install_global_hooks()
        try:
            crew = MemoryAwareCrew(
                ...,
                step_callback=adapter.make_step_callback(),
                task_callback=adapter.make_task_callback(),
            )
            result = crew.kickoff()
            return result
        except GuardrailDeny as e:
            return f"⚠️ 安全策略终止：{e.reason}"
        finally:
            adapter.cleanup()
    return agent_fn
```
Runner 保持不变，只需在外层 `_handle` 中持 `_crew_exec_lock`（ADR-v3-001）。

#### ADR-v3-004：Sub-Crew 缺少 Hook 保护

**问题**（Architecture Agent）：Sub-Crew（由 `skill_crew.py` 构建）在独立的 MCP sandbox 中执行，不经过 Main Crew 的 Hook 管道。SandboxGuard、PermissionGate、CostGuard 均不保护 Sub-Crew 的工具调用。

**设计修订**：Sub-Crew 构建时共享 Main Crew 的 HookRegistry 引用，注册独立的 adapter。但考虑到 Sub-Crew 执行在 sandbox 容器内，且已有 MCP 白名单（v2 F7），权限控制的优先级低于 Main Crew。**第一版不强制要求 Sub-Crew Hook 保护**，标记为 v3.1 迭代目标。在 `security.yaml` 中增加声明：
```yaml
sub_crew_hook_protection: false  # v3.1: 待 MCP 层 Hook 支持后启用
```

#### ADR-v3-005：SandboxGuard 正则绕过风险

**问题**（Security Agent）：
- Unicode 全角字符（`．．／` = `../`）绕过 `unquote()`
- 双重编码（`%252e%252e%252f`）单次 `unquote` 无法解码
- Null byte 注入（`..%00/`）
- 命令 denylist 不完整（缺 `doas`、`pkexec`、`su -c`）

**设计修订**：
1. 在 `unquote` 前增加 Unicode NFKC 归一化：`unicodedata.normalize('NFKC', raw)`
2. 循环 `unquote` 直到输出不再变化（最多 3 轮，防止无限循环）
3. Null byte 检查：`if '\x00' in tool_input: raise GuardrailDeny`
4. 扩展 `_DANGEROUS_COMMANDS`：增加 `doas`、`pkexec`、`su\s+`
5. **明确设计定位**：SandboxGuard 是**前置速检**，不是唯一防线。v2 的 `Path.resolve().is_relative_to()` 和 MCP 白名单是后置边界。正则检查的定位是"快速阻断 80% 的低级攻击"，残余风险由后置边界兜底。

#### ADR-v3-006：dispatch_gate 安全 handler 异常应 fail-closed

**问题**（Security Agent）：`dispatch_gate` 对非 `GuardrailDeny` 异常（如 `TypeError`、`KeyError`）执行吞掉策略——安全 handler 因 bug 抛异常时，工具调用照常进行。

**设计修订**：为安全关键 handler 增加 `fail_closed=True` 选项：
```python
def register(self, event_type, handler, name="", fail_closed=False):
    self._handlers[event_type].append((handler, fail_closed))

def dispatch_gate(self, event_type, context):
    for handler, fail_closed in self._handlers[event_type]:
        try:
            handler(context)
        except GuardrailDeny:
            raise
        except Exception as e:
            if fail_closed:
                raise GuardrailDeny(f"Security handler failed (fail-closed): {e}")
            print(f"handler error: {e}", file=sys.stderr)
```
SandboxGuard 和 PermissionGate 注册时 `fail_closed=True`，CostGuard 和 RetryTracker 保持 `fail_closed=False`。

#### ADR-v3-007：ASK 权限级别语义澄清

**问题**（Security Agent + TDD Agent）：`ASK` 级别在当前实现中等同于 `ALLOW`——仅记录日志，不阻断也不交互确认。在飞书 bot 场景没有实时审批通道。

**设计修订**：将 `ASK` 重命名为 `WARN`，明确其语义为"允许执行 + 记录审计日志 + 触发 Prometheus 告警指标"。保留三级模型 `DENY > WARN > ALLOW`，但不再暗示交互式审批。未来 v3.1 如果飞书支持卡片审批回调，再引入真正的 `ASK`。

#### ADR-v3-008：CostGuard 应复用 v2 token_counter

**问题**（Architecture Agent）：CostGuard 使用 `max(1, len(text) * 2 // 3)` 粗估 token，而 v2 已有三级 tokenizer（Qwen 官方 > HuggingFace > rough）。

**设计修订**：CostGuard 接受可选的 `token_counter` 依赖注入。在 XiaoPaw 中，通过 `deps` 机制注入 v2 的 `count_tokens()` 函数。在独立演示（m5l31）中降级使用粗估。

### 12.3 MEDIUM 问题（实现时注意）

| # | 问题 | 来源 | 处置 |
|---|------|------|------|
| M1 | Langfuse flush 无超时/重试 | Architect | Langfuse SDK 内部有 15s 超时；flush 在 SESSION_END 用 `dispatch`（fire-and-forget），不阻塞主流程。接受现状。 |
| M2 | 审计路径含 `routing_key` 中的冒号 | Architect | `routing_key.replace(':', '_')` 做路径安全化。 |
| M3 | SSOT 文档 `hook-events.md` / `strategies.md` 未创建 | Architect | 实现阶段同步创建。 |
| M4 | 审计日志无加密链 | Security | 当前单节点部署风险可控。v3.1 考虑 HMAC 链。 |
| M5 | F13 总开关关闭后所有保护失效 | Security | `assert_all_production_safe()` 已强制 F13=true。运行时 SIGHUP reload 不允许修改 F13。 |
| M6 | Strategy loader YAML `strategies:` 段在参考实现中不存在 | Code-Review | 需从零实现。已在 m5l31/m5l32 的 loader.py 中有部分逻辑，需整合。 |
| M7 | `step_callback` 组合（adapter + verbose 模式）| Code-Review | 创建 `compose_callbacks(*fns)` 工具函数，串联多个 callback。 |
| M8 | `pending_deny` 通过 `akickoff` 的传播路径未验证 | Code-Review | 需在 TDD 阶段实测 CrewAI `akickoff` 的异常传播行为。标记为 P0 集成测试。 |

### 12.4 测试设计中发现的设计隐患

> 详见 [13-test-design-hook-hardening.md](13-test-design-hook-hardening.md) §6

| # | 隐患 | 影响 | 处置 |
|---|------|------|------|
| TD-1 | m5l32 参考实现中 `@before_tool_call` 使用 `dispatch` 而非 `dispatch_gate` | 安全策略（SandboxGuard/PermissionGate）的 deny 被吞掉 | **必须修正**：XiaoPaw 实现中使用 `dispatch_gate` |
| TD-2 | AFTER_TURN 事件缺少 token 信息（adapter 中未填充 `input_tokens`/`output_tokens`）| CostGuard 在 `after_turn_handler` 中无法累加 token | **必须修正**：adapter 从 CrewAI `step_callback` 参数中提取 token 信息 |
| TD-3 | HookContext `frozen=True` 但 `tool_input: dict` 内部可变 | handler 可修改 `ctx.tool_input['key'] = 'value'` 影响后续 handler | 记录为已知限制；Python frozen dataclass 不深冻结可变字段 |
| TD-4 | LoopDetector MD5 哈希碰撞概率 | 16 字符 hex = 64 位，短 session 内碰撞概率极低 | 接受现状，记录为设计约束 |

### 12.5 Review 总结

| 严重级别 | 数量 | 状态 |
|---------|------|------|
| CRITICAL | 2 | ADR-v3-001, ADR-v3-002 已修订 |
| HIGH | 6 | ADR-v3-003 ~ ADR-v3-008 已修订 |
| MEDIUM | 8 | M1-M8 已标注处置方案 |
| 测试隐患 | 4 | TD-1 ~ TD-4 已标注 |

**下一步**：解决 §13 中 E2E 反向验证发现的设计缺口后，按 TDD 流程实现代码。

---

## 13. E2E 反向验证——用集成测试用例审视设计缺口（ADR-v3-E2E）

> **验证日期**：2026-04-24
> **方法**：用 [14-e2e-test-design.md](14-e2e-test-design.md) 的 93 条 E2E 用例逐条反向审查 §1-12 设计 + v1 实际代码
> **结论**：发现 **13 个设计缺口**，影响 **~52 条** E2E 用例。其中 CRITICAL 2 个、HIGH 6 个、MEDIUM 5 个。

### 13.1 缺口总览

| Gap ID | 根因 | 影响用例数 | 严重性 |
|--------|------|-----------|--------|
| E2E-G-001 | 测试基础设施无 Hook 观测采集机制 | 全部 93 条 | CRITICAL |
| E2E-G-002 | SandboxGuard 作用在 tool_input 而非用户消息——安全 E2E 的确定性无法保证 | ~30 条安全用例 | CRITICAL |
| E2E-G-003 | `BLOCKED_PATTERNS` 在 v1 代码中**不存在**——记忆投毒防御纯靠 LLM 指令 | E2E-F-050/051/052 | HIGH |
| E2E-G-004 | Workspace 记忆文件全局共享——跨用户隔离在 workspace 层失效 | E2E-F-070/071, E2E-A-040 | HIGH |
| E2E-G-005 | GuardrailDeny 无标准 reason code，用户回复文案不可断言 | E2E-F-002/030~035/060/062 | HIGH |
| E2E-G-006 | PermissionGate 测试引用 `shell_executor` / `email_sender`——v1 无此工具 | E2E-F-040/041 | HIGH |
| E2E-G-007 | SecureToolWrapper 无具体工具-凭证映射，集成路径空白 | E2E-F-090/091/092 | HIGH |
| E2E-G-008 | 无 Prometheus 指标测试查询手段 | E2E-F-040~042/060~062, E2E-X-006 | HIGH |
| E2E-G-009 | HookContext 无 sender_id 字段——无法区分 Cron 与用户触发 | E2E-E-010/011, E2E-X-003 | MEDIUM |
| E2E-G-010 | `_crew_exec_lock` 串行化 Crew——"同时处理 3 个消息"不可能并行 | E2E-X-004 | MEDIUM |
| E2E-G-011 | `compose_callbacks` 未设计——verbose 回调与 adapter 回调冲突 | E2E-A-022, E2E-C-020 | MEDIUM |
| E2E-G-012 | `llm_assert()` LLM-as-Judge 工具未设计 | ~9 条语义断言用例 | MEDIUM |
| E2E-G-013 | 审计文件路径含冒号（routing_key 格式）——跨平台兼容问题 | 所有审计验证用例 | MEDIUM |

### 13.2 CRITICAL 缺口修订

#### ADR-v3-E2E-001：测试基础设施——Hook 事件采集层

**问题**：§10.4 要求每条 E2E 测试验证 BEFORE_TURN 等事件触发（stderr JSON 日志）、安全审计文件、Prometheus 指标。当前设计中：
- 结构化日志输出到 stderr，测试无法从 TestAPI 响应中获取
- CaptureSender 仅捕获回复文本，无 Hook 事件采集
- 审计日志写磁盘但测试不知道路径
- Prometheus 指标需额外 HTTP 查询

**设计修订**：新增 **HookEventCollector** 测试 fixture 层（仅测试环境激活）：

```python
class HookEventCollector:
    """注册到 HookRegistry 的测试专用 handler，在内存中收集所有事件。"""
    def __init__(self):
        self.events: list[HookContext] = []
    
    def collect(self, ctx: HookContext):
        self.events.append(ctx)
    
    def filter(self, event_type: EventType) -> list[HookContext]:
        return [e for e in self.events if e.event_type == event_type]
    
    def has_event(self, event_type: EventType, **kwargs) -> bool:
        for e in self.filter(event_type):
            if all(getattr(e, k, None) == v for k, v in kwargs.items()):
                return True
        return False
```

**集成方式**：
1. `build_agent_fn()` 检测 `config.debug.enable_test_api == True` 时，将 `HookEventCollector` 注册到 registry 的**所有 7 种事件**上
2. `CaptureSender` 扩展，增加 `events: list[HookContext]` 字段，adapter cleanup 时将 collector 的事件复制过去
3. TestAPI 的 `TestResponse` 扩展：
```python
class TestResponse(BaseModel):
    msg_id: str
    reply: str
    session_id: str
    duration_ms: int
    skills_called: list[str] = []
    # v3 新增
    hook_events: list[dict] = []           # HookContext 序列化列表
    security_audit_events: list[dict] = [] # 安全审计事件
    guardrail_deny: Optional[str] = None   # GuardrailDeny reason（如有）
```
4. `SecurityAuditReader` fixture：测试结束后读取 `data/security_audit/{safe_routing_key}/audit.jsonl`

**测试客户端层级更新**：

| 客户端 | 新增能力 |
|--------|---------|
| `slash_client` | 无变化（Slash 不走 Hook） |
| `llm_client` | + `hook_events` 在响应中可见 |
| `memory_client` | + `hook_events` + `security_audit_events` |
| `full_client` | + 全部观测字段 |

#### ADR-v3-E2E-002：安全 E2E 的确定性测试策略

**问题**：安全 E2E 测试（F-010~F-092）发送用户消息如"帮我读取文件 ../../etc/passwd"，期望 SandboxGuard 拦截。但实际路径是：

```
用户消息 → LLM 推理 → (可能)生成工具调用 → SandboxGuard 检查 tool_input
```

LLM 可能直接拒绝而不生成工具调用，导致 SandboxGuard 从未触发——测试表面"通过"（没有泄露信息），但不是因为 SandboxGuard 工作，而是 LLM 碰巧拒绝了。

**设计修订——三层测试策略**：

| 层级 | 测试目标 | LLM 行为 | 适用用例 |
|------|---------|---------|---------|
| **L1：单元级** | SandboxGuard 正则检测能力 | 无 LLM | UT-SBX-* (已在 doc13 覆盖) |
| **L2：集成级** | Hook 链路：dispatch_gate → SandboxGuard → GuardrailDeny → pending_deny | Mock LLM（确定性生成工具调用） | IT-SEC-* (已在 doc13 覆盖) |
| **L3：E2E 级** | 真实 LLM + Hook 全链路 | 真实 Qwen | E2E-F-* (本文档) |

**L3 的确定性保障**：
1. **双重断言**：安全 E2E 不仅断言"回复不包含敏感信息"，还断言 `security_audit_events` 中存在拦截记录（如有）或 `hook_events` 中无 tool_call 事件（LLM 自行拒绝）
2. **标记 `@pytest.mark.llm_dependent`**：安全 E2E 标记为 LLM 行为依赖测试，CI 中允许软失败 + 人工 review
3. **补充确定性安全集成测试**：在 `tests/integration/test_security_chain.py` 中增加 **直接构造 tool_input 的确定性测试**，覆盖所有 F-* 攻击向量，不依赖 LLM。这些是真正的安全保障测试，E2E 是辅助验证

**更新 §10 测试策略**：安全保障的核心验证在 L1/L2（确定性），L3（E2E）是"加分项"而非"必须通过"。

### 13.3 HIGH 缺口修订

#### ADR-v3-E2E-003：记忆投毒防御——从 LLM 指令升级为确定性过滤

**问题**：v1 的 `memory-save` Skill 依赖 LLM 指令（SKILL.md 文本）来过滤恶意内容。没有任何 `BLOCKED_PATTERNS` 正则或代码级过滤。E2E-F-050/051/052 假设存在确定性过滤。

**设计修订**：
1. SandboxGuard 新增 `_PROMPT_INJECTION` 正则模式：
```python
_PROMPT_INJECTION = re.compile(
    r'\[(SYSTEM|INST|/INST)\]|'
    r'<\|?(system|im_start|im_end)\|?>|'
    r'忽略(之前|以上|所有)(的)?指令|'
    r'ignore\s+(previous|all|above)\s+instructions',
    re.IGNORECASE
)
```
2. SandboxGuard 在 `BEFORE_TOOL_CALL` 中检查 tool_name 为 `skill_loader` 且 tool_input 中 `task_description` 包含 `memory-save` 相关关键词时，对整个 input 做 `_PROMPT_INJECTION` 正则检查
3. 这不替代 `memory-save` Skill 自身的 LLM 指令防护——两层共存（确定性正则 + LLM 理解）
4. `soul.md` 写保护独立于 SandboxGuard：在 v2 的 `memory-save` SKILL.md 中，`soul` 目标的写入条件已经限制只能追加不能覆盖。v3 在 SandboxGuard 中增加对 `soul.md` 路径的显式写保护

**影响**：E2E-F-050 改为双重验证——SandboxGuard 正则 + LLM 行为；E2E-F-052 增加路径级写保护验证

#### ADR-v3-E2E-004：Workspace 记忆隔离——per-routing_key 目录

**问题**：v1 的 `data/workspace/` 下 `soul.md`、`user.md`、`memory.md` 等文件全局共享。E2E-F-070/071 期望跨用户数据隔离，E2E-A-040 期望"Bob 不知道 Alice 的信息"——但如果 Alice 写入 `user.md`，Bob 的 session 也能读到。

**设计修订**：
1. v3 将 workspace 拆分为 **per-routing_key 隔离目录**：
```
data/workspace/
├── _shared/              # 共享配置（soul.md 全局版）
│   └── soul.md
├── p2p_ou_alice/         # Alice 的 workspace
│   ├── soul.md           # 继承 _shared/soul.md + 个性化
│   ├── user.md
│   ├── agent.md
│   └── memory.md
├── p2p_ou_bob/           # Bob 的 workspace
│   ├── soul.md
│   ├── user.md
│   └── ...
└── group_oc_team1/       # 群聊 workspace
    └── ...
```
2. `Bootstrap.load()` 修改：先读 `_shared/soul.md`，再读 per-routing_key 的 `soul.md`（存在则覆盖）
3. `memory-save` Skill 的 sandbox mount 路径从全局 `data/workspace/` 改为 `data/workspace/{safe_routing_key}/`
4. `safe_routing_key = routing_key.replace(':', '_')`（同 M2）
5. **迁移策略**：首次运行时，如果 per-routing_key 目录不存在，从全局 `data/workspace/` 复制初始文件

**影响**：E2E-A-040/F-070/F-071 可正确验证隔离；E2E-B-020/021 的记忆跨 session 持久化也在正确的 routing_key 下

#### ADR-v3-E2E-005：GuardrailDeny 标准 Reason Code

**问题**：测试期望特定回复文案（"预算超限"、"安全拒绝"），但 GuardrailDeny 的消息字符串未标准化。

**设计修订**：

```python
class DenyReason:
    BUDGET_EXCEEDED = "budget_exceeded"
    LOOP_DETECTED = "loop_detected"
    SANDBOX_VIOLATION = "sandbox_violation"
    PERMISSION_DENIED = "permission_denied"
    PROMPT_INJECTION = "prompt_injection"

class GuardrailDeny(Exception):
    def __init__(self, reason_code: str, detail: str = ""):
        self.reason_code = reason_code
        self.detail = detail
        super().__init__(f"[{reason_code}] {detail}")
```

**用户回复映射表**（Runner 层）：

| reason_code | 中文回复 |
|-------------|---------|
| `budget_exceeded` | "⚠️ 本轮对话成本已达上限，请开始新对话" |
| `loop_detected` | "⚠️ 检测到重复操作，已自动终止" |
| `sandbox_violation` | "⚠️ 检测到不安全的操作请求，已拦截" |
| `permission_denied` | "⚠️ 该操作未获授权" |
| `prompt_injection` | "⚠️ 检测到异常指令，已拦截" |

**测试断言更新**：E2E 测试从文案关键词匹配改为检查 `TestResponse.guardrail_deny` 的 `reason_code`

#### ADR-v3-E2E-006：PermissionGate 测试工具——注册测试桩工具

**问题**：`shell_executor` / `email_sender` 在 v1 中不存在，LLM 无法生成这些工具的调用。

**设计修订**：
1. 在 `security.yaml` 中保留这些工具的权限声明（作为"如果未来添加这些工具"的预注册策略）
2. E2E 测试修改为使用 **确定性集成测试**（L2）来验证 PermissionGate 的 DENY 行为——直接构造 `BEFORE_TOOL_CALL` 事件，tool_name="shell_executor"
3. E2E 层（L3）的 F-040/F-041 改写为验证 LLM 面对危险请求时的行为——即使 PermissionGate 不触发（因为工具不存在），LLM 也不应执行 shell 命令或发送包含密码的邮件

#### ADR-v3-E2E-007：SecureToolWrapper 具体集成映射

**问题**：§5.4 的 SecureToolWrapper 缺乏具体工具-凭证映射。

**设计修订**：

| 工具 | 凭证参数 | 环境变量 | 注入时机 |
|------|---------|---------|---------|
| `BaiduSearchTool` | `api_key` | `BAIDU_SEARCH_API_KEY` | `build_agent_fn()` 中构建工具后 |
| `AliyunLLM` | `api_key` | `DASHSCOPE_API_KEY` | `build_agent_fn()` 中构建 LLM 后（注：LLM 不走 CrewAI tool 注册，已有独立凭证机制，不需 wrapper） |
| sandbox MCP tools | N/A | N/A | sandbox 容器独立环境，不暴露宿主环境变量 |

**实际需要 wrapping 的工具**：仅 `BaiduSearchTool`（当它作为 Main Agent 的直接工具注册时）。当前 v1 中 `BaiduSearchTool` 未直接注册到 Main Agent（通过 `baidu_search` Skill 间接使用），所以 v3 首版不需要 SecureToolWrapper。

**v3 首版决策**：SecureToolWrapper 保留为设计储备，不在首版实现。E2E-F-090/091/092 的验证点改为：
- F-090：sandbox 容器不包含宿主环境变量（v2 已有容器隔离）
- F-091：SandboxGuard 拦截 `config.yaml` 路径读取
- F-092：Main Agent 的 tool schema 中不包含 API key 参数（凭证在 config 层注入，不经过 LLM）

#### ADR-v3-E2E-008：Prometheus 指标测试查询

**问题**：E2E 测试需要验证 Prometheus 指标，但无查询手段。

**设计修订**：
1. v3 的策略类（CostGuard / LoopDetector / PermissionGate / SandboxGuard）统一暴露 `get_metrics() -> dict` 方法
2. `HookEventCollector` 在 cleanup 时自动收集所有已注册策略的 metrics
3. `TestResponse` 的 `hook_events` 中包含 `{"event": "session_end", "strategy_metrics": {...}}` 汇总
4. 不通过 `/metrics` HTTP 端点查询——通过内存级直接采集，避免引入网络依赖

### 13.4 MEDIUM 缺口修订

#### ADR-v3-E2E-009：HookContext 增加 sender_id 字段

```python
@dataclass(frozen=True)
class HookContext:
    # ... 现有字段 ...
    sender_id: str = ""    # v3.1 新增：消息发送者标识（"cron" / "ou_xxx"）
```

`InboundMessage.sender_id` 传入 HookContext。Handler 可通过 `ctx.sender_id == "cron"` 区分触发来源。

#### ADR-v3-E2E-010：E2E-X-004 并发测试语义修正

E2E-X-004 修正为验证 **隔离性** 而非 **并行性**。测试步骤：
1. 依次（非同时）发送 3 个不同 routing_key 的消息
2. 验证每个回复独立且不互踩
3. 测试描述改为"各自独立处理，不互踩（通过 _crew_exec_lock 串行化保证安全）"

#### ADR-v3-E2E-011：compose_callbacks 设计

```python
def compose_callbacks(*fns):
    """串联多个 step/task callback，按顺序执行。任一 callback 抛异常则中断后续。"""
    def composed(output):
        for fn in fns:
            fn(output)
    return composed
```

使用方式（在 `build_agent_fn` 中）：
```python
step_cb = compose_callbacks(
    adapter.make_step_callback(),    # Hook 事件分发（优先）
    _make_verbose_callback(sender),  # verbose 模式发送（可选）
)
```

#### ADR-v3-E2E-012：llm_assert() 测试工具设计

```python
async def llm_assert(reply: str, assertion: str, model: str = "qwen-turbo") -> bool:
    """LLM-as-Judge：用轻量模型判断回复是否满足语义断言。"""
    prompt = f"判断以下回复是否满足条件。只回答 YES 或 NO。\n\n回复：{reply}\n条件：{assertion}"
    result = await call_llm(prompt, model=model)
    return result.strip().upper().startswith("YES")
```

- 使用 `qwen-turbo`（成本最低）做判断
- 标记 `@pytest.mark.llm_dependent`
- CI 中非阻塞（允许 LLM 判断偶尔不稳定）

#### ADR-v3-E2E-013：审计文件路径安全化

统一使用 `safe_routing_key()` 工具函数：
```python
def safe_routing_key(routing_key: str) -> str:
    return routing_key.replace(":", "_").replace("/", "_")
```
审计路径：`data/security_audit/{safe_routing_key}/audit.jsonl`

### 13.5 E2E 用例修订汇总

基于以上 ADR，需同步更新 [14-e2e-test-design.md](14-e2e-test-design.md)：

| 用例/区域 | 修改内容 |
|-----------|---------|
| §2.1 TestResponse | 增加 `hook_events`, `security_audit_events`, `guardrail_deny` 字段 |
| §2.3 断言策略 | 增加 Hook 事件断言（通过 TestResponse 内存级采集，非 stderr 解析）|
| §2.4 Hook 观测 | 重写为通过 `HookEventCollector` 采集，非 stderr 抓取 |
| E2E-F-040/041 | 降级为 L2 集成测试（构造 tool_input）；E2E 层改为验证 LLM 拒绝行为 |
| E2E-F-050/051/052 | 增加 SandboxGuard `_PROMPT_INJECTION` 正则验证；保留 LLM 行为验证 |
| E2E-F-060/062 | 断言从文案匹配改为 `guardrail_deny == "budget_exceeded"` / `"loop_detected"` |
| E2E-F-070/071 | 前置条件增加 per-routing_key workspace 隔离 |
| E2E-F-090/091/092 | 调整验证点为容器隔离 + SandboxGuard 路径拦截 |
| E2E-X-004 | 从"同时处理"改为"依次处理，验证隔离性" |
| 安全 E2E 全部 | 增加 `@pytest.mark.llm_dependent` 标记 + 确定性 L2 兜底测试 |

### 13.6 缺口总结

| 严重级别 | 数量 | 状态 |
|---------|------|------|
| CRITICAL | 2 | ADR-v3-E2E-001, 002 已修订 |
| HIGH | 6 | ADR-v3-E2E-003 ~ 008 已修订 |
| MEDIUM | 5 | ADR-v3-E2E-009 ~ 013 已修订 |

**修订后影响**：原 52 条可能失败的 E2E 用例中：
- 全部 93 条通过 ADR-v3-E2E-001（TestResponse 扩展）获得 Hook 观测能力
- ~30 条安全 E2E 通过 ADR-v3-E2E-002（三层测试策略）明确测试层级和确定性保障
- 剩余用例通过各 ADR 的具体设计修订获得可实现路径

**下一步**：更新 14-e2e-test-design.md 反映以上修订 → 基于 13+14 文档按 TDD 流程实现代码。
