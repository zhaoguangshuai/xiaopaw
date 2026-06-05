"""Runner: per-routing_key serial queue with gen-counter worker lifecycle.

v3 integration: Hook framework fires 5+2 events around agent execution.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from xiaopaw.feishu.session_key import routing_type
from xiaopaw.hook_framework.crew_adapter import CrewObservabilityAdapter, set_current_adapter
from xiaopaw.hook_framework.registry import EventType, GuardrailDeny, HookContext, HookRegistry
from xiaopaw.models import InboundMessage, SenderProtocol
from xiaopaw.observability.metrics import agent_latency, inbound_total
from xiaopaw.observability.trace import bind_trace_id
from xiaopaw.session.manager import SessionManager
from xiaopaw.session.models import MessageEntry

logger = logging.getLogger(__name__)

AgentFn = Callable[
    [str, list[MessageEntry], str, str, bool],
    Awaitable[str],
]

_SLASH_COMMANDS = {"/new", "/help", "/status", "/verbose"}


class Runner:
    def __init__(
        self,
        session_mgr: SessionManager,
        sender: SenderProtocol,
        agent_fn: AgentFn,
        idle_timeout: float = 300.0,
        max_queue_size: int = 10,
        data_dir: Path | None = None,
        hook_registry: HookRegistry | None = None,
    ) -> None:
        self._session_mgr = session_mgr
        self._sender = sender
        self._agent_fn = agent_fn
        self._idle_timeout = idle_timeout
        self._max_queue_size = max_queue_size
        self._data_dir = data_dir or Path("data")

        self._hook_registry = hook_registry

        self._queues: dict[str, asyncio.Queue[InboundMessage]] = {}
        self._workers: dict[str, asyncio.Task] = {}
        self._queue_gen: dict[str, int] = {}
        self._dispatch_lock = asyncio.Lock()
        self._pending_index_tasks: set[asyncio.Task] = set()
        self._shutting_down = False

    async def dispatch(self, inbound: InboundMessage) -> None:
        if self._shutting_down:
            logger.warning("dispatch rejected (shutting down): %s", inbound.routing_key)
            return

        async with self._dispatch_lock:
            key = inbound.routing_key
            if key not in self._queues:
                self._queues[key] = asyncio.Queue(maxsize=self._max_queue_size)
                self._queue_gen[key] = 0

            q = self._queues[key]
            if q.full():
                logger.warning("queue full for %s, dropping message", key)
                return

            await q.put(inbound)

            if key not in self._workers or self._workers[key].done():
                self._queue_gen[key] += 1
                gen = self._queue_gen[key]
                self._workers[key] = asyncio.create_task(
                    self._worker(key, gen), name=f"worker-{key}"
                )

    async def _worker(self, key: str, gen: int) -> None:
        logger.info("worker started: %s (gen=%d)", key, gen)
        try:
            while True:
                try:
                    inbound = await asyncio.wait_for(
                        self._queues[key].get(), timeout=self._idle_timeout
                    )
                except asyncio.TimeoutError:
                    break

                await self._handle(inbound)

        except Exception:
            logger.exception("worker error: %s", key)
        finally:
            if self._queue_gen.get(key) == gen:
                self._workers.pop(key, None)
                self._queues.pop(key, None)
                self._queue_gen.pop(key, None)
                logger.info("worker exited: %s (gen=%d, cleaned up)", key, gen)
            else:
                logger.info("worker exited: %s (gen=%d, superseded)", key, gen)

    async def _handle(self, inbound: InboundMessage) -> None:
        token = bind_trace_id(inbound.trace_id)
        start = time.monotonic()
        key = inbound.routing_key

        adapter: CrewObservabilityAdapter | None = None
        card_msg_id: str | None = None
        try:
            # Slash command intercept
            cmd = inbound.content.strip().split()[0].lower() if inbound.content.strip() else ""
            if cmd in _SLASH_COMMANDS:
                reply = await self._handle_slash(cmd, inbound)
                await self._sender.send(key, reply)
                return

            # Get or create session
            session = await self._session_mgr.get_or_create(key)

            # ★ 接线点 1：为本次请求创建 Hook adapter（每请求一个，session_id 绑定）
            # adapter 在本函数内通过 ContextVar 传递给 main_crew、skill_loader、sub-crew
            if self._hook_registry:
                adapter = CrewObservabilityAdapter(
                    registry=self._hook_registry,
                    session_id=session.id,
                )

            # Hook: BEFORE_TURN —— 触发 structured_log + langfuse_trace 创建 trace
            if adapter:
                adapter.on_turn_start(
                    user_message=inbound.content,
                    sender_id=inbound.sender_id,
                )

            # Load history
            history = await self._session_mgr.load_history(session.id)

            # Send thinking indicator, save card_msg_id for later update
            card_msg_id = await self._sender.send_thinking(key)

            # ★ 接线点 2：pre-flight 安全检查
            # 把整个 Agent 执行包成一个虚拟工具调用 "agent_execution"，
            # 让 sandbox_guard / permission_gate 对用户原始输入提前过一遍 ——
            # 否则恶意 prompt 要等到 LLM 决定调真实工具时才会被拦截，浪费 LLM 算力。
            #
            # 因为 BEFORE_TOOL_CALL 抛 GuardrailDeny 会被 adapter 的 pending_deny 吞掉
            # （pending_deny 模式见 crew_adapter），这里手动检查并立即重抛，
            # 让外层的 except GuardrailDeny 捕获并向用户回复"安全策略拦截"。
            if adapter:
                adapter.on_before_tool_call(
                    tool_name="agent_execution",
                    tool_input={"content": inbound.content[:500]},
                )
                if adapter._pending_deny:
                    pending = adapter._pending_deny
                    adapter._pending_deny = None
                    raise pending

            # Run agent (with adapter available via ContextVar for internal crew hooks)
            adapter_token = set_current_adapter(adapter) if adapter else None
            try:
                reply = await self._agent_fn(
                    inbound.content,
                    history,
                    session.id,
                    key,
                    session.verbose,
                )
            finally:
                if adapter_token is not None:
                    set_current_adapter(None)

            # Hook: AFTER_TOOL_CALL for the agent execution
            if adapter:
                adapter.on_after_tool_call(
                    tool_name="agent_execution",
                    tool_input={"content": inbound.content[:500]},
                    tool_result=reply[:500],
                )

            # Send reply: update the thinking card if available, else send new card
            if card_msg_id:
                await self._sender.update_card(card_msg_id, reply)
            else:
                await self._sender.send(key, reply)

            # Persist conversation
            await self._session_mgr.append(
                session.id,
                user=inbound.content,
                feishu_msg_id=inbound.msg_id,
                assistant=reply,
                ts=inbound.ts,
            )

            elapsed = time.monotonic() - start
            agent_latency.labels(routing_type=routing_type(key)).observe(elapsed)

            # Hook: AFTER_TURN
            if adapter and self._hook_registry:
                self._hook_registry.dispatch(
                    EventType.AFTER_TURN,
                    HookContext(
                        event_type=EventType.AFTER_TURN,
                        session_id=session.id,
                        sender_id=inbound.sender_id,
                        duration_ms=elapsed * 1000,
                        metadata={
                            "user_message": inbound.content[:500],
                            "reply": reply[:500],
                        },
                    ),
                )

        except GuardrailDeny as deny:
            # ★ 接线点 3：兜底捕获 GuardrailDeny —— 友好告知用户而不是 500 错误
            # GuardrailDeny 的来源有三处：
            #   1. pre-flight 检查（上面的 raise pending）
            #   2. main_crew 内部 step_callback / task_callback 重抛
            #   3. cleanup() 时的 SESSION_END handler
            elapsed = time.monotonic() - start
            logger.warning("guardrail deny for %s: %s", key, deny)
            deny_reply = f"安全策略拦截：{deny.detail or deny.reason_code}"

            if adapter and self._hook_registry:
                self._hook_registry.dispatch(
                    EventType.AFTER_TURN,
                    HookContext(
                        event_type=EventType.AFTER_TURN,
                        session_id=adapter._session_id,
                        sender_id=inbound.sender_id,
                        duration_ms=elapsed * 1000,
                        metadata={
                            "user_message": inbound.content[:500],
                            "reply": deny_reply,
                            "guardrail_deny": True,
                            "deny_reason": deny.reason_code,
                            "deny_detail": deny.detail,
                        },
                    ),
                )

            try:
                if card_msg_id:
                    await self._sender.update_card(card_msg_id, deny_reply)
                else:
                    await self._sender.send_text(key, deny_reply)
            except Exception:
                pass
        except Exception:
            logger.exception("handle error for %s", key)
            error_reply = "抱歉，处理消息时出现了错误，请稍后重试。"
            try:
                if card_msg_id:
                    await self._sender.update_card(card_msg_id, error_reply)
                else:
                    await self._sender.send_text(key, error_reply)
            except Exception:
                pass
        finally:
            # ★ 接线点 4：finally 触发 SESSION_END
            # adapter.cleanup() 内部 dispatch SESSION_END → 触发：
            #   - audit_logger.session_end_handler（写本会话安全摘要）
            #   - langfuse_trace.flush_and_close（强制 flush，机制五）
            # 必须在 finally 里 —— 即使 except 分支已经 send 了回复给用户，
            # 我们仍要保证 Langfuse 数据落盘
            if adapter:
                try:
                    adapter.cleanup()
                except GuardrailDeny:
                    # cleanup 也可能抛 deny（pending_deny 重抛），但用户已经收到回复
                    # 这里的 deny 只用于 audit/log，吞掉即可
                    pass
            bind_trace_id("-")

    async def _handle_slash(self, cmd: str, inbound: InboundMessage) -> str:
        key = inbound.routing_key

        if cmd == "/new":
            session = await self._session_mgr.create_new_session(key)
            return f"已创建新会话 {session.id}"

        if cmd == "/help":
            return (
                "可用命令：\n"
                "  /new — 创建新会话\n"
                "  /status — 查看当前会话状态\n"
                "  /verbose on|off — 开关详细模式\n"
                "  /help — 显示此帮助"
            )

        if cmd == "/status":
            session_info = self._session_mgr.get_session_info(key)
            if session_info:
                return (
                    f"会话 ID: {session_info.id}\n"
                    f"创建时间: {session_info.created_at}\n"
                    f"消息数: {session_info.message_count}\n"
                    f"详细模式: {'开启' if session_info.verbose else '关闭'}"
                )
            return "当前无活动会话"

        if cmd == "/verbose":
            parts = inbound.content.strip().split()
            on = parts[1].lower() in ("on", "1", "true") if len(parts) > 1 else True
            await self._session_mgr.update_verbose(key, on)
            return f"详细模式已{'开启' if on else '关闭'}"

        return f"未知命令: {cmd}"

    async def shutdown(self) -> None:
        self._shutting_down = True
        logger.info("runner shutting down...")

        for task in self._workers.values():
            task.cancel()
        if self._workers:
            await asyncio.gather(*self._workers.values(), return_exceptions=True)

        for task in self._pending_index_tasks:
            task.cancel()
        if self._pending_index_tasks:
            await asyncio.gather(*self._pending_index_tasks, return_exceptions=True)

        self._workers.clear()
        self._queues.clear()
        logger.info("runner shutdown complete")
