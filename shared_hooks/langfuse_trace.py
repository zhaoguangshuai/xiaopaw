"""Langfuse 全链路追踪 —— 把 5+2 Hook 事件翻译为 Langfuse trace 树。

【Trace 树的目标层次结构】
    Trace (id = session_id)                ← 机制一：多轮对话同 trace
      └─ root span: session-{sid}
          └─ tool-agent_execution         ← 包裹整个 Crew 执行
              ├─ GENERATION（每次 LLM 调用）  ← 机制四：先写后更新
              │    └─ TOOL span（每次工具调用）← 机制三：span 栈管理父子
              └─ task-complete span

【五大机制对应代码位置】
机制一：多轮对话同棵树 → _get_trace_id()（trace_id = session_id）
机制二：Sub-crew 自动挂父 trace → ContextVar + copy_context() 自动传播（在 crew_adapter）
机制三：Span 栈维护嵌套关系 → _span_stack_var（不可变元组栈，LIFO）
机制四：Generation 先写后更新 → before_llm_handler() 处理上一个 gen 的 close
机制五：强制 flush 保证可见性 → after_turn_handler() 末尾调用 _flush_batch()

【为什么用 SDK v4 + ContextVar】
- SDK v4 的 ingestion.batch() 是显式批处理，便于精确控制 flush 时机
- ContextVar 而非全局变量：thread-safe + 子线程通过 copy_context() 自动继承
- 不可变元组栈：copy_context() 复制 ContextVar 时只复制引用，列表会被多线程共享出 bug
"""

import atexit
import logging
import os
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from threading import Lock

logger = logging.getLogger(__name__)

_ENABLED = os.environ.get("TRACE_TO_LANGFUSE", "").lower() in ("1", "true")

try:
    from xiaopaw.observability.trace import trace_id_var as _ext_trace_id_var
except ImportError:
    _ext_trace_id_var: ContextVar[str] = ContextVar("trace_id", default="-")

_client = None
_init_failed = False
_batch_buffer: list = []
_batch_lock = Lock()

_trace_id_var: ContextVar[str] = ContextVar("lf_trace_id", default="")
_session_id_var: ContextVar[str] = ContextVar("lf_session_id", default="")
_root_span_id_var: ContextVar[str] = ContextVar("lf_root_span_id", default="")
_gen_id_var: ContextVar[str] = ContextVar("lf_gen_id", default="")
_gen_count_var: ContextVar[int] = ContextVar("lf_gen_count", default=0)
_tool_count_var: ContextVar[int] = ContextVar("lf_tool_count", default=0)
_span_stack_var: ContextVar[tuple] = ContextVar("lf_span_stack", default=())
_closed_spans_var: ContextVar[dict] = ContextVar("lf_closed_spans", default={})


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
            logger.warning("langfuse init failed (non-blocking)", exc_info=True)
            return None
    return _client


def _now():
    return datetime.now(timezone.utc)


def _uid():
    return str(uuid.uuid4())


def _enqueue(event):
    with _batch_lock:
        _batch_buffer.append(event)


def _flush_batch():
    global _batch_buffer
    with _batch_lock:
        if not _batch_buffer:
            return
        batch = _batch_buffer[:]
        _batch_buffer = []

    client = _ensure_client()
    if client is None:
        return

    chunk_size = 50
    for i in range(0, len(batch), chunk_size):
        chunk = batch[i : i + chunk_size]
        try:
            client.api.ingestion.batch(batch=chunk)
        except Exception:
            logger.debug("langfuse batch ingestion failed", exc_info=True)


def _get_trace_id(ctx) -> str:
    """★ 机制一：让多轮对话留在同一棵 trace 树。

    核心思想：trace_id = session_id（不用随机 UUID）。
    Langfuse 的 trace-create 是 upsert 操作 —— 相同 ID 第二次调用是更新，不是新建。
    所以 Turn 1 / Turn 2 / Turn N 都用同一个 session_id 作为 trace_id，
    在 Langfuse 里自动合并到一棵树。

    优先用外部注入的 trace_id（_ext_trace_id_var），兼容上游已有 tracing 的场景；
    没有就 fallback 到 session_id。
    """
    trace_id = _ext_trace_id_var.get("-")
    if trace_id == "-":
        trace_id = ctx.session_id
    return trace_id or ""


def _extract_recent_tool_results(prompt_messages: list) -> list[tuple[str, str]]:
    """Extract tool results after the last assistant message in prompt_messages.

    Returns [(tool_name, content), ...] in chronological order.
    Preserves duplicates for positional matching against span stack.
    """
    results: list[tuple[str, str]] = []
    for msg in reversed(prompt_messages or []):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "")
        if role == "tool":
            name = msg.get("name", "")
            if name:
                results.append((name, msg.get("content", "")))
        elif role == "assistant":
            break
    results.reverse()
    return results


def _extract_prev_llm_output(prompt_messages: list) -> dict | None:
    """Extract the previous LLM call's output from the current prompt_messages.

    Walks backward through messages to find the last assistant message before
    the current tool-result block. Works with both OpenAI and Qwen message formats
    (Qwen tool results use tool_call_id instead of name).
    """
    if not prompt_messages:
        return None
    for msg in reversed(prompt_messages):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "")
        if role == "tool":
            continue
        if role == "assistant":
            tool_calls = msg.get("tool_calls")
            content = msg.get("content")
            if tool_calls and isinstance(tool_calls, list):
                return {
                    "action": "tool_calls",
                    "tools": [
                        {
                            "name": tc.get("function", {}).get("name", ""),
                            "arguments": tc.get("function", {}).get("arguments", ""),
                        }
                        for tc in tool_calls
                    ],
                }
            if content:
                return {"reply": str(content)[:500]}
            return None
        break
    return None


def _get_tool_parent_id() -> str:
    """★ 机制三：tool span 的父节点。

    优先级：当前 generation > span 栈顶 > root span
    LLM 调用工具时，工具自然挂在那次 LLM 调用（generation）之下。
    嵌套调用时，外层 tool 在栈底，内层 tool 在栈顶 —— LIFO 天然匹配嵌套。
    """
    gen_id = _gen_id_var.get("")
    if gen_id:
        return gen_id
    stack = _span_stack_var.get(())
    if stack:
        return stack[-1][0]
    return _root_span_id_var.get("")


def _get_gen_parent_id() -> str:
    """★ 机制三：generation span 的父节点 —— 不能挂在当前 gen 上。

    优先级：span 栈顶 > root span
    Generation 不能挂另一个 generation（违反 Langfuse 的 trace 模型）。
    如果在 sub-crew 子线程里，栈顶是父线程的 tool-skill_name span ——
    sub-crew 的 LLM 调用就会自动成为父 skill span 的子节点（机制二的关键）。
    """
    stack = _span_stack_var.get(())
    if stack:
        return stack[-1][0]
    return _root_span_id_var.get("")


def _ensure_trace(ctx):
    if _trace_id_var.get(""):
        return _trace_id_var.get()

    client = _ensure_client()
    if client is None:
        return None

    trace_id = _get_trace_id(ctx)
    if not trace_id:
        return None

    _trace_id_var.set(trace_id)
    _session_id_var.set(ctx.session_id)

    from langfuse.api import CreateSpanBody, TraceBody
    from langfuse.api.ingestion.types import (
        IngestionEvent_SpanCreate,
        IngestionEvent_TraceCreate,
    )

    _enqueue(
        IngestionEvent_TraceCreate(
            id=_uid(),
            timestamp=_now().isoformat(),
            type="trace-create",
            body=TraceBody(
                id=trace_id,
                name=f"xiaopaw-session-{ctx.session_id}",
                session_id=ctx.session_id,
                metadata={"source": "xiaopaw-v2"},
            ),
        )
    )

    root_id = _uid()
    _root_span_id_var.set(root_id)

    _enqueue(
        IngestionEvent_SpanCreate(
            id=_uid(),
            timestamp=_now().isoformat(),
            type="span-create",
            body=CreateSpanBody(
                id=root_id,
                trace_id=trace_id,
                name=f"session-{ctx.session_id}",
                start_time=_now(),
                metadata={"session_id": ctx.session_id, "source": "xiaopaw-v2"},
            ),
        )
    )

    return trace_id


def before_turn_handler(ctx) -> None:
    if not _ENABLED:
        return
    _ensure_trace(ctx)

    _gen_count_var.set(0)
    _gen_id_var.set("")
    _tool_count_var.set(0)
    _closed_spans_var.set({})

    user_message = ctx.metadata.get("user_message", "")
    trace_id = _trace_id_var.get("")
    if trace_id and user_message:
        from langfuse.api import TraceBody
        from langfuse.api.ingestion.types import IngestionEvent_TraceCreate

        _enqueue(
            IngestionEvent_TraceCreate(
                id=_uid(),
                timestamp=_now().isoformat(),
                type="trace-create",
                body=TraceBody(
                    id=trace_id,
                    input={"message": user_message},
                    user_id=ctx.sender_id or None,
                ),
            )
        )


def before_llm_handler(ctx) -> None:
    """★ 机制四：Generation 先写后更新。

    系统里没有 AFTER_LLM 事件，所以 generation 的关闭分两个时机：
    1. 下一次 BEFORE_LLM（本函数顶部）：关闭上一个 gen，并补全期间发生的 tool span 的 output
    2. AFTER_TURN（after_turn_handler 末尾）：关闭本轮最后一个 gen

    【为什么"先写后更新"是必要的】
    LLM 调用开始时只知道 input；它什么时候结束、output 是什么，
    要等到 Agent 拿到工具结果再次调用 LLM 时，才能从 prompt_messages 里反推：
        - 上一个 LLM 的 output = 上一个 assistant message
        - 期间触发的 tool 调用结果 = 后续 tool messages
    本函数前半部分就是这个反推过程：扫描 span 栈 + 匹配 tool messages，
    补全 tool span 的 output 并 close 它，再 close 上一个 generation。
    """
    if not _ENABLED:
        return
    _ensure_trace(ctx)

    prev_gen_id = _gen_id_var.get("")
    if prev_gen_id:
        from langfuse.api import UpdateGenerationBody, UpdateSpanBody
        from langfuse.api.ingestion.types import (
            IngestionEvent_GenerationUpdate,
            IngestionEvent_SpanUpdate,
        )

        stack = list(_span_stack_var.get(()))
        tool_results = _extract_recent_tool_results(
            ctx.metadata.get("prompt_messages", [])
        )

        remaining_stack = []
        closed_entries = []
        used_indices: set[int] = set()
        closed = dict(_closed_spans_var.get({}))

        for entry in stack:
            span_id, tool_name, turn_num = entry[0], entry[1], entry[2]
            matched_content = None
            for i, (rname, rcontent) in enumerate(tool_results):
                if i not in used_indices and rname == tool_name:
                    matched_content = rcontent
                    used_indices.add(i)
                    break

            if matched_content is not None:
                span_output = {"result": matched_content}
                closed[(tool_name, turn_num)] = span_id
                closed_entries.append(entry)
                _enqueue(
                    IngestionEvent_SpanUpdate(
                        id=_uid(),
                        timestamp=_now().isoformat(),
                        type="span-update",
                        body=UpdateSpanBody(
                            id=span_id,
                            output=span_output,
                            end_time=_now(),
                            metadata={"phase": "auto-closed-by-next-llm"},
                        ),
                    )
                )
            else:
                remaining_stack.append(entry)

        _closed_spans_var.set(closed)
        _span_stack_var.set(tuple(remaining_stack))

        # Extract the previous LLM's actual output from the message history.
        # This works for both tool-call patterns and direct text responses,
        # and handles Qwen format (no "name" on tool result messages).
        prompt_messages = ctx.metadata.get("prompt_messages", [])
        gen_output = _extract_prev_llm_output(prompt_messages)
        # Fallback: use closed span names if message-based extraction failed
        if not gen_output and closed_entries:
            gen_output = {
                "action": "tool_calls",
                "tools": [
                    {"name": e[1], "input": e[3] if len(e) > 3 else {}}
                    for e in closed_entries
                ],
            }

        close_kwargs: dict = {"id": prev_gen_id, "end_time": _now()}
        if gen_output:
            close_kwargs["output"] = gen_output
        _enqueue(
            IngestionEvent_GenerationUpdate(
                id=_uid(),
                timestamp=_now().isoformat(),
                type="generation-update",
                body=UpdateGenerationBody(**close_kwargs),
            )
        )
        _gen_id_var.set("")

    count = _gen_count_var.get(0) + 1
    _gen_count_var.set(count)

    gen_id = _uid()
    _gen_id_var.set(gen_id)

    prompt_messages = ctx.metadata.get("prompt_messages", [])
    prompt_preview = ctx.metadata.get("prompt_preview", "")
    gen_input = None
    if prompt_messages:
        gen_input = {"messages": prompt_messages}
    elif prompt_preview:
        gen_input = {"prompt": prompt_preview}

    model = ctx.metadata.get("model", "") or "qwen3-max"

    from langfuse.api import CreateGenerationBody
    from langfuse.api.ingestion.types import IngestionEvent_GenerationCreate

    _enqueue(
        IngestionEvent_GenerationCreate(
            id=_uid(),
            timestamp=_now().isoformat(),
            type="generation-create",
            body=CreateGenerationBody(
                id=gen_id,
                trace_id=_trace_id_var.get(""),
                parent_observation_id=_get_gen_parent_id(),
                name=f"llm-call-{count}",
                model=model,
                start_time=_now(),
                input=gen_input,
                metadata={
                    "agent_id": ctx.agent_id,
                    "turn": ctx.turn_number,
                    "call_number": count,
                },
            ),
        )
    )


def before_tool_handler(ctx) -> None:
    if not _ENABLED:
        return
    _ensure_trace(ctx)

    tool_input = dict(ctx.tool_input) if ctx.tool_input else {}
    _tool_count_var.set(_tool_count_var.get(0) + 1)

    span_id = _uid()

    from langfuse.api import CreateSpanBody
    from langfuse.api.ingestion.types import IngestionEvent_SpanCreate

    _enqueue(
        IngestionEvent_SpanCreate(
            id=_uid(),
            timestamp=_now().isoformat(),
            type="span-create",
            body=CreateSpanBody(
                id=span_id,
                trace_id=_trace_id_var.get(""),
                parent_observation_id=_get_tool_parent_id(),
                name=f"tool-{ctx.tool_name}",
                start_time=_now(),
                input=tool_input or None,
                metadata={
                    "tool_name": ctx.tool_name,
                    "turn": ctx.turn_number,
                    "phase": "attempt",
                },
            ),
        )
    )

    # ★ 机制三：把新 span 压入栈顶（不可变元组追加，确保 ContextVar 安全传播）
    # 栈元素：(span_id, tool_name, turn_number, tool_input) —— after_tool_handler 用前两项匹配
    # 用元组而不是 list：copy_context() 复制 ContextVar 时复制的是引用，
    # 列表会被多线程共享导致 sub-crew 改栈影响主线程；元组不可变，append 总是产生新元组
    old_stack = _span_stack_var.get(())
    _span_stack_var.set(
        (*old_stack, (span_id, ctx.tool_name, ctx.turn_number, tool_input))
    )


def after_tool_handler(ctx) -> None:
    if not _ENABLED:
        return

    tool_output = ctx.metadata.get("tool_output", "")
    is_deny = ctx.metadata.get("guardrail_deny", False)
    level = "ERROR" if (not ctx.success or is_deny) else "DEFAULT"

    output_body: dict = {"success": ctx.success}
    if tool_output:
        output_body["result"] = tool_output
    if is_deny:
        output_body["deny_reason"] = ctx.metadata.get("deny_reason", "")
        output_body["deny_detail"] = ctx.metadata.get("deny_detail", "")

    stack = list(_span_stack_var.get(()))
    key = (ctx.tool_name, ctx.turn_number)
    matched_span_id = None
    for i in range(len(stack) - 1, -1, -1):
        if (stack[i][1], stack[i][2]) == key:
            matched_span_id = stack.pop(i)[0]
            break
    _span_stack_var.set(tuple(stack))

    if not matched_span_id:
        closed = dict(_closed_spans_var.get({}))
        matched_span_id = closed.pop(key, None)
        if matched_span_id:
            _closed_spans_var.set(closed)

    if matched_span_id:
        from langfuse.api import UpdateSpanBody
        from langfuse.api.ingestion.types import IngestionEvent_SpanUpdate

        _enqueue(
            IngestionEvent_SpanUpdate(
                id=_uid(),
                timestamp=_now().isoformat(),
                type="span-update",
                body=UpdateSpanBody(
                    id=matched_span_id,
                    output=output_body,
                    level=level,
                    end_time=_now(),
                    metadata={
                        "tool_name": ctx.tool_name,
                        "duration_ms": ctx.duration_ms,
                        "phase": "denied" if is_deny else "completed",
                    },
                ),
            )
        )
    else:
        _ensure_trace(ctx)
        tool_input = dict(ctx.tool_input) if ctx.tool_input else {}
        span_id = _uid()

        from langfuse.api import CreateSpanBody
        from langfuse.api.ingestion.types import IngestionEvent_SpanCreate

        _enqueue(
            IngestionEvent_SpanCreate(
                id=_uid(),
                timestamp=_now().isoformat(),
                type="span-create",
                body=CreateSpanBody(
                    id=span_id,
                    trace_id=_trace_id_var.get(""),
                    parent_observation_id=_get_tool_parent_id(),
                    name=f"tool-{ctx.tool_name}",
                    start_time=_now(),
                    end_time=_now(),
                    input=tool_input or None,
                    output=output_body,
                    level=level,
                    metadata={
                        "tool_name": ctx.tool_name,
                        "duration_ms": ctx.duration_ms,
                        "phase": "denied" if is_deny else "completed",
                    },
                ),
            )
        )


def after_turn_handler(ctx) -> None:
    """★ 机制四 + 机制五：关闭最后的 generation/span，强制 flush 到 Langfuse。

    本函数末尾的 _flush_batch() 是关键：
    必须在 sender.send(reply) 之前完成，保证用户拿到回复时 Langfuse 数据已就绪。

    is_intermediate=True 的 turn 是 step_callback 触发的中间 turn —— 不 flush，
    只有真正的轮次结束（通常是 task_callback 之后）才执行清理与 flush。
    """
    if not _ENABLED:
        return

    if ctx.metadata.get("is_intermediate", False):
        return

    _ensure_trace(ctx)

    output = ctx.metadata.get("reply", "") or ctx.metadata.get("output", "")

    gen_id = _gen_id_var.get("")
    if gen_id:
        from langfuse.api import UpdateGenerationBody
        from langfuse.api.ingestion.types import IngestionEvent_GenerationUpdate

        update_kwargs: dict = {"id": gen_id, "end_time": _now()}
        if output:
            update_kwargs["output"] = output
        if ctx.input_tokens or ctx.output_tokens:
            from langfuse.api.commons.types.usage import Usage

            update_kwargs["usage"] = Usage(
                input=ctx.input_tokens,
                output=ctx.output_tokens,
                total=ctx.input_tokens + ctx.output_tokens,
            )

        _enqueue(
            IngestionEvent_GenerationUpdate(
                id=_uid(),
                timestamp=_now().isoformat(),
                type="generation-update",
                body=UpdateGenerationBody(**update_kwargs),
            )
        )
        _gen_id_var.set("")

    stack = list(_span_stack_var.get(()))
    if stack:
        from langfuse.api import UpdateSpanBody
        from langfuse.api.ingestion.types import IngestionEvent_SpanUpdate

        for entry in stack:
            _enqueue(
                IngestionEvent_SpanUpdate(
                    id=_uid(),
                    timestamp=_now().isoformat(),
                    type="span-update",
                    body=UpdateSpanBody(
                        id=entry[0],
                        end_time=_now(),
                        metadata={"phase": "auto-closed-by-after-turn"},
                    ),
                )
            )
        _span_stack_var.set(())

    trace_id = _trace_id_var.get("")
    if trace_id:
        from langfuse.api import TraceBody
        from langfuse.api.ingestion.types import IngestionEvent_TraceCreate

        meta: dict = {"source": "xiaopaw-v2"}
        if ctx.duration_ms:
            meta["duration_ms"] = ctx.duration_ms
        if ctx.input_tokens or ctx.output_tokens:
            meta["usage"] = {
                "input_tokens": ctx.input_tokens,
                "output_tokens": ctx.output_tokens,
                "total_tokens": ctx.input_tokens + ctx.output_tokens,
            }
        model = ctx.metadata.get("model", "")
        if model:
            meta["model"] = model
        if ctx.metadata.get("guardrail_deny"):
            meta["guardrail_deny"] = True
            meta["deny_reason"] = ctx.metadata.get("deny_reason", "")

        body_kwargs: dict = {"id": trace_id, "metadata": meta}
        if output:
            body_kwargs["output"] = {"reply": output}

        _enqueue(
            IngestionEvent_TraceCreate(
                id=_uid(),
                timestamp=_now().isoformat(),
                type="trace-create",
                body=TraceBody(**body_kwargs),
            )
        )

    root_id = _root_span_id_var.get("")
    if root_id and output:
        from langfuse.api import UpdateSpanBody
        from langfuse.api.ingestion.types import IngestionEvent_SpanUpdate

        _enqueue(
            IngestionEvent_SpanUpdate(
                id=_uid(),
                timestamp=_now().isoformat(),
                type="span-update",
                body=UpdateSpanBody(id=root_id, output={"reply": output}),
            )
        )

    _flush_batch()


def task_complete_handler(ctx) -> None:
    if not _ENABLED:
        return
    _ensure_trace(ctx)

    task_desc = ctx.metadata.get("task_description", ctx.task_name)
    raw_output = ctx.metadata.get("raw_output", "")

    from langfuse.api import CreateSpanBody
    from langfuse.api.ingestion.types import IngestionEvent_SpanCreate

    stack = _span_stack_var.get(())
    root_id = _root_span_id_var.get("")
    parent_id = stack[-1][0] if stack else root_id

    _enqueue(
        IngestionEvent_SpanCreate(
            id=_uid(),
            timestamp=_now().isoformat(),
            type="span-create",
            body=CreateSpanBody(
                id=_uid(),
                trace_id=_trace_id_var.get(""),
                parent_observation_id=parent_id or None,
                name="task-complete",
                start_time=_now(),
                end_time=_now(),
                input=task_desc or None,
                output=raw_output or None,
                metadata={"agent": ctx.agent_id},
            ),
        )
    )


def subcrew_cleanup() -> None:
    """★ 机制二：sub-crew 子线程结束时的清理。

    sub-crew 在 ThreadPoolExecutor 子线程里执行，结束时本函数被调用：
    1. 关闭 sub-crew 内遗留的 generation 和 span（避免 trace 树里的"幽儽节点"）
    2. flush 子线程 buffer 里累积的 Langfuse 事件

    【关键约束：不重置 ContextVar】
    虽然把 _gen_id_var 设成 "" 看似合理，但 ContextVar 是共享给父线程的引用 ——
    如果在子线程里 reset，会破坏父线程的上下文（让父 Crew 后续调用看不到自己的 gen）。
    所以这里只 close span 不重置 ContextVar，依赖父线程的 after_turn_handler 自然清理。
    """
    if not _ENABLED:
        return

    gen_id = _gen_id_var.get("")
    if gen_id:
        from langfuse.api import UpdateGenerationBody
        from langfuse.api.ingestion.types import IngestionEvent_GenerationUpdate

        _enqueue(
            IngestionEvent_GenerationUpdate(
                id=_uid(),
                timestamp=_now().isoformat(),
                type="generation-update",
                body=UpdateGenerationBody(id=gen_id, end_time=_now()),
            )
        )
        _gen_id_var.set("")

    stack = _span_stack_var.get(())
    if stack:
        from langfuse.api import UpdateSpanBody
        from langfuse.api.ingestion.types import IngestionEvent_SpanUpdate

        for entry in stack:
            _enqueue(
                IngestionEvent_SpanUpdate(
                    id=_uid(),
                    timestamp=_now().isoformat(),
                    type="span-update",
                    body=UpdateSpanBody(
                        id=entry[0],
                        end_time=_now(),
                        metadata={"phase": "subcrew-cleanup"},
                    ),
                )
            )
        _span_stack_var.set(())

    _flush_batch()


def flush_and_close(ctx) -> None:
    """★ 机制五：SESSION_END 触发的最终 flush —— 由 hooks.yaml 挂在 SESSION_END。

    runner 在 finally 中调用 adapter.cleanup() → 触发 SESSION_END → 本函数：
    1. 关闭整个 session 仍未 close 的 span（兜底）
    2. 强制把 buffer 里的全部事件推送到 Langfuse
    3. 在 sender.send(reply) 之前完成 —— 用户看到回复时 trace 已可见
    """
    if not _ENABLED:
        return

    stack = _span_stack_var.get(())
    if stack:
        from langfuse.api import UpdateSpanBody
        from langfuse.api.ingestion.types import IngestionEvent_SpanUpdate

        for entry in stack:
            _enqueue(
                IngestionEvent_SpanUpdate(
                    id=_uid(),
                    timestamp=_now().isoformat(),
                    type="span-update",
                    body=UpdateSpanBody(
                        id=entry[0],
                        level="WARNING",
                        status_message="orphaned-span-auto-closed",
                        end_time=_now(),
                    ),
                )
            )
        _span_stack_var.set(())

    gen_id = _gen_id_var.get("")
    if gen_id:
        from langfuse.api import UpdateGenerationBody
        from langfuse.api.ingestion.types import IngestionEvent_GenerationUpdate

        _enqueue(
            IngestionEvent_GenerationUpdate(
                id=_uid(),
                timestamp=_now().isoformat(),
                type="generation-update",
                body=UpdateGenerationBody(id=gen_id, end_time=_now()),
            )
        )
        _gen_id_var.set("")

    _ensure_trace(ctx)
    trace_id = _trace_id_var.get("")
    root_id = _root_span_id_var.get("")

    if trace_id:
        from langfuse.api import CreateSpanBody
        from langfuse.api.ingestion.types import IngestionEvent_SpanCreate

        _enqueue(
            IngestionEvent_SpanCreate(
                id=_uid(),
                timestamp=_now().isoformat(),
                type="span-create",
                body=CreateSpanBody(
                    id=_uid(),
                    trace_id=trace_id,
                    parent_observation_id=root_id or None,
                    name="session_end",
                    start_time=_now(),
                    end_time=_now(),
                    metadata={
                        "event": "session_end",
                        "session_id": ctx.session_id,
                    },
                ),
            )
        )

    if root_id:
        from langfuse.api import UpdateSpanBody
        from langfuse.api.ingestion.types import IngestionEvent_SpanUpdate

        _enqueue(
            IngestionEvent_SpanUpdate(
                id=_uid(),
                timestamp=_now().isoformat(),
                type="span-update",
                body=UpdateSpanBody(id=root_id, end_time=_now()),
            )
        )

    _flush_batch()

    _trace_id_var.set("")
    _session_id_var.set("")
    _root_span_id_var.set("")
    _gen_id_var.set("")
    _gen_count_var.set(0)
    _tool_count_var.set(0)
    _closed_spans_var.set({})
