"""FeishuSender: send messages to Feishu with retry and rate-limit awareness."""

from __future__ import annotations

import asyncio
import json
import logging

from xiaopaw.models import SenderProtocol
from xiaopaw.observability.metrics import feishu_rate_limit_total

logger = logging.getLogger(__name__)

FEISHU_RATE_LIMIT_CODES = {99991663, 99991672, 99991671}
FEISHU_HTTP_RATE_LIMIT_STATUS = {429}


class FeishuSender:
    def __init__(
        self,
        client,
        max_retries: int = 3,
        retry_backoff: tuple[float, ...] = (1.0, 2.0, 4.0),
        max_concurrent: int = 5,
    ) -> None:
        self._client = client
        self._max_retries = max_retries
        self._backoff = retry_backoff
        self._sem = asyncio.Semaphore(max_concurrent)

    def _build_card(self, text: str) -> str:
        card = {
            "config": {"wide_screen_mode": True},
            "elements": [
                {
                    "tag": "div",
                    "text": {"content": text, "tag": "lark_md"},
                }
            ],
        }
        return json.dumps(card, ensure_ascii=False)

    async def send(self, routing_key: str, content: str) -> str:
        async with self._sem:
            return await self._send_with_retry(
                routing_key, self._build_card(content), msg_type="interactive"
            )

    async def send_thinking(self, routing_key: str, text: str = "⏳ 思考中，请稍候...") -> str | None:
        async with self._sem:
            try:
                return await self._send_with_retry(
                    routing_key, self._build_card(text), msg_type="interactive"
                )
            except Exception:
                logger.debug("send_thinking failed (non-critical)")
                return None

    async def update_card(self, card_msg_id: str, content: str) -> None:
        async with self._sem:
            try:
                await self._update_card_with_retry(card_msg_id, self._build_card(content))
            except Exception:
                logger.warning("update_card failed for %s", card_msg_id)

    async def send_text(self, routing_key: str, text: str) -> None:
        async with self._sem:
            await self._send_with_retry(
                routing_key, json.dumps({"text": text}), msg_type="text"
            )

    async def _send_with_retry(
        self, routing_key: str, content: str, msg_type: str
    ) -> str:
        from lark_oapi.api.im.v1 import (
            CreateMessageRequest,
            CreateMessageRequestBody,
        )

        chat_type, chat_id = self._parse_routing_key(routing_key)
        receive_id_type = "open_id" if chat_type == "p2p" else "chat_id"

        body = CreateMessageRequestBody.builder() \
            .receive_id(chat_id) \
            .msg_type(msg_type) \
            .content(content) \
            .build()

        request = CreateMessageRequest.builder() \
            .receive_id_type(receive_id_type) \
            .request_body(body) \
            .build()

        last_exc: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                response = await asyncio.to_thread(
                    self._client.im.v1.message.create, request
                )
                if response.code in FEISHU_RATE_LIMIT_CODES:
                    feishu_rate_limit_total.inc()
                    if attempt < self._max_retries - 1:
                        delay = self._backoff[min(attempt, len(self._backoff) - 1)]
                        await asyncio.sleep(delay)
                        continue
                if not response.success():
                    logger.warning("feishu send error: code=%d msg=%s", response.code, response.msg)
                return response.data.message_id if response.data else ""
            except Exception as exc:
                last_exc = exc
                if attempt < self._max_retries - 1:
                    delay = self._backoff[min(attempt, len(self._backoff) - 1)]
                    await asyncio.sleep(delay)

        raise last_exc or RuntimeError("feishu send failed")

    async def _update_card_with_retry(self, card_msg_id: str, content: str) -> None:
        from lark_oapi.api.im.v1 import PatchMessageRequest, PatchMessageRequestBody

        body = PatchMessageRequestBody.builder().content(content).build()
        request = PatchMessageRequest.builder() \
            .message_id(card_msg_id) \
            .request_body(body) \
            .build()

        for attempt in range(self._max_retries):
            try:
                response = await asyncio.to_thread(
                    self._client.im.v1.message.patch, request
                )
                if response.success():
                    return
                logger.warning(
                    "update_card error (attempt %d): code=%d msg=%s",
                    attempt, response.code, response.msg,
                )
                if attempt < self._max_retries - 1:
                    await asyncio.sleep(self._backoff[min(attempt, len(self._backoff) - 1)])
            except Exception:
                if attempt < self._max_retries - 1:
                    await asyncio.sleep(self._backoff[min(attempt, len(self._backoff) - 1)])

    @staticmethod
    def _parse_routing_key(routing_key: str) -> tuple[str, str]:
        parts = routing_key.split(":", 1)
        if len(parts) != 2:
            return "p2p", routing_key
        return parts[0], parts[1]
