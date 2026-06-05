"""结构化事件日志 —— 每个 Hook 事件输出一行 JSON 到 stderr。

【设计要点】
1. **每事件一行 JSON**：可被 fluentd / Loki / 自研日志管道直接消费，无需解析
2. **挂在 hooks 段**（不是 strategies）：用 dispatch 而非 dispatch_gate，
   日志写失败绝不能影响业务
3. **写到 stderr 而非 stdout**：stdout 留给 Agent 实际响应；stderr 是诊断通道
4. **handler 是模块级函数**：无状态，所有 7 个事件各对应一个 handler 函数

【与 langfuse_trace 的关系】
本文件是"轻量本地化"的可观测，langfuse_trace.py 是"重型云端化"的可观测。
两者并行挂载：哪怕 Langfuse 出问题，本地日志仍然完整。
"""

import json
import sys
from datetime import datetime, timezone


def _emit(event_data: dict) -> None:
    event_data["timestamp"] = datetime.now(timezone.utc).isoformat()
    try:
        print(json.dumps(event_data, ensure_ascii=False, default=str), file=sys.stderr)
    except Exception:
        pass


def before_turn_handler(ctx) -> None:
    _emit({
        "event": "before_turn",
        "session_id": ctx.session_id,
        "turn": ctx.turn_number,
        "agent_id": ctx.agent_id,
    })


def before_llm_handler(ctx) -> None:
    _emit({
        "event": "before_llm",
        "session_id": ctx.session_id,
        "turn": ctx.turn_number,
        "agent_id": ctx.agent_id,
        "input_tokens": ctx.input_tokens,
    })


def before_tool_handler(ctx) -> None:
    _emit({
        "event": "before_tool_call",
        "session_id": ctx.session_id,
        "turn": ctx.turn_number,
        "tool_name": ctx.tool_name,
        "tool_input_preview": str(dict(ctx.tool_input))[:200] if ctx.tool_input else "",
    })


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
