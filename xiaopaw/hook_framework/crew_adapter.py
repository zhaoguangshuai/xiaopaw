"""CrewAI 回调 → 5+2 事件体系的翻译层。

【为什么需要这个 adapter】
CrewAI 自带的 hook 体系（@before_tool_use 装饰器、step_callback、task_callback）
不是为加固设计的，存在三个问题需要这一层抹平：

1. CrewAI 会吞掉 @before_tool_use 抛出的异常（视为"工具调用失败重试"），
   导致 GuardrailDeny 抛了等于没抛 → 用 pending_deny 字段把它存起来，
   到 step_callback / task_callback 这些"安全出口"再重抛。

2. CrewAI 没有"turn 开始/结束"概念，只有"step"——
   adapter 用 _current_turn_has_llm 标记位推断 turn 边界。

3. CrewAI 的工具时延没有内置度量——adapter 在 BEFORE/AFTER_TOOL_CALL 之间用
   time.monotonic() 自己算 duration_ms。

【事件映射表】
  on_turn_start     → BEFORE_TURN     （runner 主动调用，每轮一次）
  on_before_llm     → BEFORE_LLM      （CrewAI @before_llm_call 触发）
  on_before_tool_call → BEFORE_TOOL_CALL  （dispatch_gate + pending_deny）
  on_after_tool_call  → AFTER_TOOL_CALL
  step_callback     → AFTER_TURN      （安全出口，重抛 pending_deny）
  task_callback     → TASK_COMPLETE   （安全出口，重抛 pending_deny）
  cleanup()         → SESSION_END     （runner finally 触发，flush Langfuse）
"""

import os
import time
from contextvars import ContextVar
from typing import Callable

from .registry import EventType, GuardrailDeny, HookContext, HookRegistry

# ContextVar 让 adapter 在子线程（Sub-Crew）里也能被找到——
# Python 的 copy_context() 会把当前线程的 ContextVar 复制给子线程，
# 这是 Sub-Crew 能自动挂到父 trace 上的基础（参见 langfuse_trace.py "机制二"）
_current_adapter: ContextVar["CrewObservabilityAdapter | None"] = ContextVar(
    "current_hook_adapter", default=None
)


def set_current_adapter(adapter: "CrewObservabilityAdapter | None"):
    return _current_adapter.set(adapter)


def get_current_adapter() -> "CrewObservabilityAdapter | None":
    return _current_adapter.get(None)

_MAX_TEXT = 2000


def _truncate(text: str, limit: int = _MAX_TEXT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"... [truncated, {len(text)} chars total]"


class CrewObservabilityAdapter:
    """每个 session 一个实例，把 CrewAI 回调翻译为 5+2 事件。

    【生命周期】
    runner 在每轮对话开始时实例化（也可全 session 复用），
    工具调用过程中累积 _pending_deny，在 step_callback / task_callback 重抛，
    最后由 runner 的 finally 调用 cleanup() 触发 SESSION_END。

    【字段释义】
    - _turn_count：本次会话的轮次（从 1 开始）
    - _current_turn_has_llm：标记位，用于推断 turn 边界
    - _pending_deny：被 CrewAI 吞掉的 GuardrailDeny 暂存，等安全出口重抛
    - _tool_start_times：(tool_name, turn) → start_time，用来算工具耗时
    """

    def __init__(self, registry: HookRegistry, session_id: str = ""):
        self._registry = registry
        self._session_id = session_id
        self._turn_count = 0
        self._current_turn_has_llm = False
        self._cleaned = False
        # ★ pending_deny：CrewAI 会吞掉 BEFORE_TOOL_CALL 的异常，
        #   所以 deny 不能立即抛，得存起来等到 step_callback 这种安全出口再重抛
        self._pending_deny: GuardrailDeny | None = None
        self._last_agent_role = ""
        self._last_prompt_preview = ""
        self._tool_start_times: dict[tuple[str, int], float] = {}

    def on_turn_start(
        self, user_message: str = "", sender_id: str = ""
    ):
        self._turn_count += 1
        self._current_turn_has_llm = True
        self._registry.dispatch(
            EventType.BEFORE_TURN,
            HookContext(
                event_type=EventType.BEFORE_TURN,
                agent_id="runner",
                session_id=self._session_id,
                turn_number=self._turn_count,
                sender_id=sender_id,
                metadata={"user_message": _truncate(user_message, 500)},
            ),
        )

    def on_before_llm(self, agent_role: str = "", messages: list | None = None, model: str = ""):
        self._last_agent_role = agent_role

        if not self._current_turn_has_llm:
            self._turn_count += 1
            self._current_turn_has_llm = True
            self._registry.dispatch(
                EventType.BEFORE_TURN,
                HookContext(
                    event_type=EventType.BEFORE_TURN,
                    agent_id=agent_role,
                    session_id=self._session_id,
                    turn_number=self._turn_count,
                ),
            )

        prompt_messages = []
        if messages:
            for m in messages[-10:]:
                if isinstance(m, dict):
                    entry = {
                        "role": m.get("role", ""),
                        "content": _truncate(str(m.get("content", "")), 300),
                    }
                    if m.get("role") == "tool" and m.get("name"):
                        entry["name"] = m["name"]
                    prompt_messages.append(entry)
                else:
                    prompt_messages.append({"content": _truncate(str(m), 300)})

        preview = ""
        if messages:
            last_msg = messages[-1]
            content = last_msg.get("content", "") if isinstance(last_msg, dict) else str(last_msg)
            preview = _truncate(str(content), 500)
        self._last_prompt_preview = preview

        self._registry.dispatch(
            EventType.BEFORE_LLM,
            HookContext(
                event_type=EventType.BEFORE_LLM,
                agent_id=agent_role,
                session_id=self._session_id,
                turn_number=self._turn_count,
                metadata={
                    "prompt_preview": preview,
                    "prompt_messages": prompt_messages,
                    "model": model or os.environ.get("AGENT_MODEL", "qwen3-max"),
                },
            ),
        )

    def on_before_tool_call(self, tool_name: str, tool_input: dict | None = None):
        """工具调用前 —— 触发 BEFORE_TOOL_CALL 策略链（dispatch_gate）。

        【pending_deny 模式】
        sandbox_guard / permission_gate 抛 GuardrailDeny 后，本方法捕获并：
        1. 存入 _pending_deny，等 step_callback 重抛（让 CrewAI 知道该终止）
        2. 立即额外发一次 AFTER_TOOL_CALL（标记 guardrail_deny=True）——
           这是为了让 Langfuse trace 里有"被拦截的调用"的完整记录，
            即使工具实际没执行
        """
        input_dict = dict(tool_input or {})
        self._tool_start_times[(tool_name, self._turn_count)] = time.monotonic()
        ctx = HookContext(
            event_type=EventType.BEFORE_TOOL_CALL,
            tool_name=tool_name,
            tool_input=input_dict,
            session_id=self._session_id,
            turn_number=self._turn_count,
        )
        try:
            self._registry.dispatch_gate(EventType.BEFORE_TOOL_CALL, ctx)
        except GuardrailDeny as e:
            # 不能直接 raise——CrewAI 会把它当成"工具失败重试"，触发死循环
            # 存起来，等 step_callback 那个安全出口再重抛
            self._pending_deny = e
            start = self._tool_start_times.pop((tool_name, self._turn_count), None)
            deny_ms = round((time.monotonic() - start) * 1000) if start else 0
            # 即使被拦截也补一次 AFTER_TOOL_CALL，让 Langfuse 关闭这个 span
            # 否则 trace 里会留下永远 open 的"幽灵 span"
            self._registry.dispatch(
                EventType.AFTER_TOOL_CALL,
                HookContext(
                    event_type=EventType.AFTER_TOOL_CALL,
                    tool_name=tool_name,
                    tool_input=input_dict,
                    session_id=self._session_id,
                    turn_number=self._turn_count,
                    success=False,
                    duration_ms=deny_ms,
                    metadata={
                        "tool_output": f"[DENIED] {e.reason_code}: {e.detail}",
                        "guardrail_deny": True,         # ← Langfuse 据此显示拦截标记
                        "deny_reason": e.reason_code,
                        "deny_detail": e.detail,
                    },
                ),
            )

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
                duration_ms=elapsed_ms,
                metadata={"tool_output": truncated},
            ),
        )

    def dispatch_after_turn(self, output: str = "") -> None:
        """Public entry for AFTER_TURN, called from step_callback."""
        try:
            self._registry.dispatch_gate(
                EventType.AFTER_TURN,
                HookContext(
                    event_type=EventType.AFTER_TURN,
                    session_id=self._session_id,
                    turn_number=self._turn_count,
                    agent_id=self._last_agent_role,
                    metadata={
                        "output": _truncate(output),
                        "is_intermediate": True,
                    },
                ),
            )
        except GuardrailDeny as e:
            self._pending_deny = self._pending_deny or e
        self._current_turn_has_llm = False
        self._last_prompt_preview = ""

    def make_step_callback(self) -> Callable:
        """生成 CrewAI step_callback —— pending_deny 的安全出口。

        【为什么 step_callback 是安全出口】
        CrewAI 在每个推理 step 结束后会调用 step_callback，
        这里抛出的异常会被 CrewAI 正确传播到 kickoff() 的调用方（runner），
        而不像 @before_tool_use 抛的异常会被吞掉。

        所以模式是：
            BEFORE_TOOL_CALL deny → 存入 _pending_deny（不抛）
            ↓ CrewAI 继续运行（看不到异常）
            ↓ tool 真的没执行（因为加固层已经拦了）
            ↓ step 结束 → step_callback 触发
            → 重抛 _pending_deny → runner 收到 → 回复用户"安全策略拦截"

        AFTER_TURN 也在这里触发，因为 CrewAI 没有 turn 事件，借 step_callback 充当。
        """
        def callback(step):
            step_output = _truncate(str(getattr(step, "output", "") or ""))
            tool_name = getattr(step, "tool", "") or ""

            try:
                # 触发 AFTER_TURN（cost_guard 算账、loop_detector 检测循环都在这里）
                self._registry.dispatch_gate(
                    EventType.AFTER_TURN,
                    HookContext(
                        event_type=EventType.AFTER_TURN,
                        session_id=self._session_id,
                        turn_number=self._turn_count,
                        agent_id=self._last_agent_role,
                        tool_name=tool_name,
                        metadata={
                            "output": step_output,
                            "prompt_preview": self._last_prompt_preview,
                            "is_intermediate": True,
                        },
                    ),
                )
            except GuardrailDeny as e:
                # AFTER_TURN 自己也可能 deny（cost/loop），同样存起来等下面统一抛
                # 注意 "or e"：如果 BEFORE_TOOL_CALL 已经存了 deny，保留先发生的那个
                self._pending_deny = self._pending_deny or e
            self._current_turn_has_llm = False
            self._last_prompt_preview = ""

            # ★ 核心：到这里安全重抛 _pending_deny
            if self._pending_deny:
                pending = self._pending_deny
                self._pending_deny = None
                raise pending

        return callback

    def make_task_callback(self) -> Callable:
        def callback(task_output):
            raw = _truncate(str(getattr(task_output, "raw", str(task_output))))
            desc = getattr(task_output, "description", "") or ""

            self._registry.dispatch(
                EventType.TASK_COMPLETE,
                HookContext(
                    event_type=EventType.TASK_COMPLETE,
                    session_id=self._session_id,
                    task_name=_truncate(str(desc), 500),
                    agent_id=self._last_agent_role,
                    metadata={
                        "raw_output": raw,
                        "task_description": _truncate(str(desc), 500),
                    },
                ),
            )

            if self._pending_deny:
                pending = self._pending_deny
                self._pending_deny = None
                raise pending

        return callback

    def cleanup(self):
        """SESSION_END —— 由 runner 在 finally 中调用。

        【作用】
        1. 触发 audit_logger 写本会话的安全摘要（events_by_type 等聚合统计）
         2. 触发 langfuse_trace.flush_and_close —— 强制 flush
           必须在 sender.send(reply) 之前完成，保证用户看到回复时 Langfuse 数据已就绪

        【幂等保护】
        多次调用 cleanup 也不会重复触发 SESSION_END（_cleaned 标志位）
        但 pending_deny 仍然每次都会重抛 —— runner 可以借此拿到最终的拦截原因
        """
        pending = self._pending_deny
        self._pending_deny = None
        if self._cleaned:
            if pending:
                raise pending
            return
        self._cleaned = True
        self._registry.dispatch(
            EventType.SESSION_END,
            HookContext(
                event_type=EventType.SESSION_END,
                session_id=self._session_id,
            ),
        )
        if pending:
            raise pending
