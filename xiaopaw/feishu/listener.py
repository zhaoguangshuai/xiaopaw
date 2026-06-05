"""FeishuListener: WebSocket-based event listener."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from xiaopaw.feishu.session_key import resolve_routing_key
from xiaopaw.models import Attachment, InboundMessage
from xiaopaw.observability.metrics import inbound_total
from xiaopaw.observability.security import RateLimiter, ReplayCache
from xiaopaw.observability.trace import new_trace_id

logger = logging.getLogger(__name__)

OnMessage = Callable[[InboundMessage], Awaitable[None]]


class FeishuListener:
    def __init__(
        self,
        app_id: str,
        app_secret: str,
        on_message: OnMessage,
        replay_cache: ReplayCache,
        rate_limiter: RateLimiter,
        on_bot_added: Callable[[Any], Awaitable[None]] | None = None,
        allowed_chats: list[str] | None = None,
    ) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._on_message = on_message
        self._replay_cache = replay_cache
        self._rate_limiter = rate_limiter
        self._on_bot_added = on_bot_added
        self._allowed_chats = set(allowed_chats) if allowed_chats else None
        self._ws_client = None
        self._main_loop: asyncio.AbstractEventLoop | None = None

    async def start(self) -> None:
        import threading

        import lark_oapi.ws.client as ws_mod
        from lark_oapi.ws.client import Client as WsClient

        self._main_loop = asyncio.get_running_loop()
        event_handler = self._build_event_handler()
        self._ws_client = WsClient(
            app_id=self._app_id,
            app_secret=self._app_secret,
            event_handler=event_handler,
        )

        def _run_ws() -> None:
            ws_mod.loop = asyncio.new_event_loop()
            self._ws_client.start()

        t = threading.Thread(target=_run_ws, daemon=True)
        t.start()
        logger.info("feishu websocket listener started (thread=%s)", t.name)

    async def stop(self) -> None:
        if self._ws_client:
            logger.info("feishu websocket listener stopped")

    def _build_event_handler(self):
        import lark_oapi as lark

        handler = lark.EventDispatcherHandler.builder("", "")

        def _on_p2p_receive(data) -> None:
            asyncio.run_coroutine_threadsafe(
                self._handle_message_event(data), self._main_loop
            )

        handler.register_p2_im_message_receive_v1(_on_p2p_receive)
        return handler.build()

    async def _handle_message_event(self, data) -> None:
        try:
            event = data.event
            msg = event.message
            sender = event.sender

            event_id = getattr(data.header, "event_id", "") if data.header else ""
            if event_id and await self._replay_cache.seen(event_id):
                logger.debug("replay blocked: %s", event_id)
                return

            open_id = sender.sender_id.open_id if sender.sender_id else ""
            if not self._rate_limiter.allow(open_id):
                logger.warning("rate limited: %s", open_id)
                return

            chat_id = msg.chat_id or ""
            chat_type = msg.chat_type or "p2p"
            thread_id = getattr(msg, "thread_id", "") or ""

            if self._allowed_chats and chat_type != "p2p":
                if chat_id not in self._allowed_chats:
                    logger.debug("chat not in allowed list: %s", chat_id)
                    return

            routing_key = resolve_routing_key(chat_type, chat_id, open_id, thread_id)

            content_raw = msg.content or "{}"
            import json
            content_dict = json.loads(content_raw)
            text = content_dict.get("text", "")

            attachment = None
            if msg.message_type == "image":
                image_key = content_dict.get("image_key", "")
                if image_key:
                    attachment = Attachment(
                        msg_type="image", file_key=image_key, file_name=""
                    )
            elif msg.message_type == "file":
                file_key = content_dict.get("file_key", "")
                file_name = content_dict.get("file_name", "")
                if file_key:
                    attachment = Attachment(
                        msg_type="file", file_key=file_key, file_name=file_name
                    )

            inbound = InboundMessage(
                routing_key=routing_key,
                content=text,
                msg_id=msg.message_id or "",
                root_id=msg.root_id or "",
                sender_id=open_id,
                ts=int(msg.create_time or "0"),
                attachment=attachment,
                trace_id=new_trace_id(),
            )

            inbound_total.labels(
                source="feishu",
                routing_type=routing_key.split(":")[0],
            ).inc()

            await self._on_message(inbound)

        except Exception:
            logger.exception("failed to handle feishu message event")
