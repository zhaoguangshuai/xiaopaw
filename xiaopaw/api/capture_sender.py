"""CaptureSender: captures agent replies for TestAPI synchronous response."""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


class CaptureSender:
    """In-memory sender that captures replies for test client retrieval."""

    def __init__(self) -> None:
        self._futures: dict[str, asyncio.Future[str]] = {}
        self._last_card_msg_id: str = ""

    def register(self, msg_id: str) -> asyncio.Future[str]:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        self._futures[msg_id] = future
        return future

    async def send(self, routing_key: str, content: str) -> str:
        for msg_id, future in list(self._futures.items()):
            if not future.done():
                future.set_result(content)
                break
        return f"capture_{id(content)}"

    async def send_thinking(self, routing_key: str, text: str = "...") -> str | None:
        return None

    async def update_card(self, card_msg_id: str, content: str) -> None:
        self._last_card_msg_id = card_msg_id
        for msg_id, future in list(self._futures.items()):
            if not future.done():
                future.set_result(content)
                break

    async def send_text(self, routing_key: str, text: str) -> None:
        await self.send(routing_key, text)

    async def wait_for_reply(self, msg_id: str, timeout: float = 300.0) -> str:
        future = self._futures.get(msg_id)
        if not future:
            raise KeyError(f"no registered future for msg_id={msg_id}")
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._futures.pop(msg_id, None)
