"""Hook 事件分发引擎 —— 5+2 事件体系的核心。

【职责】
1. 定义 5+2 事件类型（EventType）
2. 定义 Hook 上下文（HookContext，只读 dataclass）
3. 提供两套分发机制：dispatch（报警器） + dispatch_gate（保险丝）
4. 定义 GuardrailDeny 异常（唯一能穿透 dispatch_gate 的异常）

【设计要点】
- 两套分发对应两段 hooks.yaml：观测层用 dispatch（异常吞掉），策略层用 dispatch_gate（GuardrailDeny 阻断）
- HookContext 是 frozen 的，tool_input/metadata 是 MappingProxyType——Handler 只能读不能改
"""

import sys
import traceback
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Callable


class EventType(Enum):
    """5+2 事件体系。

    5 个核心事件按 turn 生命周期顺序触发：
        BEFORE_TURN → BEFORE_LLM → BEFORE_TOOL_CALL → AFTER_TOOL_CALL → AFTER_TURN
    2 个补充事件按需触发：
        TASK_COMPLETE：CrewAI Task 完成回调
        SESSION_END：整个会话结束（runner.cleanup 时触发）
    """

    BEFORE_TURN = "before_turn"
    BEFORE_LLM = "before_llm"
    BEFORE_TOOL_CALL = "before_tool_call"
    AFTER_TOOL_CALL = "after_tool_call"
    AFTER_TURN = "after_turn"
    TASK_COMPLETE = "task_complete"
    SESSION_END = "session_end"


class DenyReason(str, Enum):
    """GuardrailDeny 的标准原因码。

    写入 Langfuse metadata 和 security_audit.jsonl，用于事后归因。
    """

    BUDGET_EXCEEDED = "budget_exceeded"      # cost_guard 抛出
    LOOP_DETECTED = "loop_detected"          # loop_detector 抛出
    SANDBOX_VIOLATION = "sandbox_violation"  # sandbox_guard 抛出，也是 fail_closed 兜底原因
    PERMISSION_DENIED = "permission_denied"  # permission_gate 抛出
    PROMPT_INJECTION = "prompt_injection"    # sandbox_guard 检测到 prompt 注入特征


class GuardrailDeny(Exception):
    """策略层"拒绝"信号 —— 唯一能穿透 dispatch_gate 的异常。

    只有这种异常能：
    1. 阻断 BEFORE_TOOL_CALL 链路（让工具不被执行）
    2. 在 step_callback 里被 pending_deny 重抛（详见 crew_adapter）
    3. 被 runner 捕获并向用户回复"安全策略拦截：xxx"

    其他异常都会被 dispatch_gate 吞掉（除非 handler 标记了 fail_closed）。
    """

    def __init__(self, reason_code: str | DenyReason, detail: str = ""):
        self.reason_code = reason_code.value if isinstance(reason_code, DenyReason) else reason_code
        self.detail = detail
        super().__init__(f"[{self.reason_code}] {detail}")


@dataclass(frozen=True)
class HookContext:
    """Hook 调用上下文 —— 不可变，Handler 只能读不能改。

    【为什么 frozen=True】
    多个 handler 串行执行时，前一个 handler 不能篡改输入污染后续 handler。
    比如 sandbox_guard 不能擅自修改 tool_input 让后续 cost_guard 看到脏数据。

    【为什么 tool_input/metadata 转 MappingProxyType】
    frozen=True 只防止整个对象被替换，但 dict 字段本身仍可变。
    MappingProxyType 是 dict 的只读代理，连 ctx.tool_input["x"] = 1 都会抛 TypeError。

    【字段约定】
    - turn_number：本次 session 的轮次计数（从 1 开始）
    - sender_id：飞书用户 open_id（用于 permission_gate 按 routing_key 鉴权）
    - metadata：handler 间约定的扩展字段（如 prompt_messages、guardrail_deny 标记）
    """

    event_type: EventType
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    agent_id: str = ""
    task_name: str = ""
    tool_name: str = ""
    tool_input: dict = field(default_factory=dict)
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: float = 0
    success: bool = True
    session_id: str = ""
    turn_number: int = 0
    sender_id: str = ""
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        # 用 object.__setattr__ 绕开 frozen 限制完成只读化封装
        object.__setattr__(self, "tool_input", MappingProxyType(dict(self.tool_input)))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


class HookRegistry:
    """Hook 注册中心 + 两套分发机制。

    【两套分发的设计动机】
    观测层（structured_log / langfuse_trace）和策略层（sandbox_guard 等）的失败语义完全不同：
    - 观测层：网络抖动、写文件失败 → 业务必须照常进行（绝不能因为 trace 写不动就拒绝用户）
    - 策略层：检测到攻击 → 必须立即阻断业务

    所以提供两个 dispatch 方法，hooks.yaml 上下两段分别使用。
    """

    def __init__(self):
        # 每个 event_type 对应一个 (handler, fail_closed) 列表
        # fail_closed 仅在 dispatch_gate 中生效：True 表示"该 handler 自己崩了 = 默认拒绝"
        self._handlers: dict[EventType, list[tuple[Callable, bool]]] = defaultdict(list)
        self._handler_names: dict[EventType, list[str]] = defaultdict(list)

    def register(
        self,
        event_type: EventType,
        handler: Callable,
        name: str = "",
        fail_closed: bool = False,
    ):
        """注册一个 handler 到指定事件。

        【注册顺序就是执行顺序】
        HookLoader 严格按 hooks.yaml 中的声明顺序调用 register()，
        因此 yaml 里的行序直接决定运行时的执行链路。
        """
        self._handlers[event_type].append((handler, fail_closed))
        self._handler_names[event_type].append(
            name or getattr(handler, "__name__", repr(handler))
        )

    def dispatch(self, event_type: EventType, context: HookContext):
        """报警器模式 —— 所有异常被吞掉，不影响业务。

        用于观测层：Langfuse 网络超时、日志文件写失败时，业务照常进行。
        即使一个 handler 崩了，后续 handler 仍会被调用。
        """
        for handler, _fail_closed in self._handlers[event_type]:
            try:
                handler(context)
            except Exception as e:
                # 把异常打到 stderr 供运维排查，但不抛出
                print(
                    f"[HookRegistry] {event_type.value} handler error: {e}\n"
                    f"{traceback.format_exc()}",
                    file=sys.stderr,
                )

    def dispatch_gate(self, event_type: EventType, context: HookContext):
        """保险丝模式 —— 只有 GuardrailDeny 能穿透，首次 deny 立即中止链路。

        用于策略层：sandbox_guard 抛 deny → permission_gate 不再执行 → 业务被阻断。

        【fail_closed 的两层含义】
        - False（默认）：handler 内部异常被吞掉（和 dispatch 一样）
        - True：handler 内部异常 → 转换为 GuardrailDeny 抛出（"安全组件坏了 = 默认拒绝"）
            sandbox_guard 这类安全 handler 必须标 fail_closed=True，
            一旦自己出 bug 也宁可错杀，不可放过。
        """
        for handler, fail_closed in self._handlers[event_type]:
            try:
                handler(context)
            except GuardrailDeny:
                # 唯一会向上传播的异常，让 crew_adapter / runner 捕获后阻断业务
                raise
            except Exception as e:
                if fail_closed:
                    # 安全 handler 自己崩溃 → 当作 deny 处理（fail-closed 默认拒绝）
                    raise GuardrailDeny(
                        DenyReason.SANDBOX_VIOLATION,
                        f"Security handler failed (fail-closed): {e}",
                    ) from e
                print(
                    f"[HookRegistry] {event_type.value} handler error: {e}",
                    file=sys.stderr,
                )

    def handler_count(self, event_type: EventType) -> int:
        return len(self._handlers[event_type])

    def summary(self) -> dict[str, list[str]]:
        """启动时打印用：列出所有事件下注册了哪些 handler，便于排查接线问题。"""
        return {
            et.value: list(names)
            for et, names in self._handler_names.items()
            if names
        }
