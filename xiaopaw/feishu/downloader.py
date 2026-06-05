"""File and image download from Feishu to local session directory."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class FeishuDownloader:
    def __init__(self, client) -> None:
        self._client = client

    async def download_attachment(
        self,
        msg_id: str,
        file_key: str,
        file_name: str,
        dest_dir: Path,
        msg_type: str = "file",
    ) -> Path | None:
        dest_dir.mkdir(parents=True, exist_ok=True)

        try:
            if msg_type == "image":
                return await self._download_image(msg_id, file_key, dest_dir)
            return await self._download_file(msg_id, file_key, file_name, dest_dir)
        except Exception:
            logger.exception("download failed: msg_id=%s file_key=%s", msg_id, file_key)
            return None

    async def _download_image(
        self, msg_id: str, image_key: str, dest_dir: Path
    ) -> Path | None:
        from lark_oapi.api.im.v1 import GetMessageResourceRequest

        request = GetMessageResourceRequest.builder() \
            .message_id(msg_id) \
            .file_key(image_key) \
            .type("image") \
            .build()

        response = await asyncio.to_thread(
            self._client.im.v1.message_resource.get, request
        )
        if not response.success():
            logger.warning("image download failed: %s", response.msg)
            return None

        dest = dest_dir / f"{image_key}.png"
        dest.write_bytes(response.file.read())
        return dest

    async def _download_file(
        self, msg_id: str, file_key: str, file_name: str, dest_dir: Path
    ) -> Path | None:
        from lark_oapi.api.im.v1 import GetMessageResourceRequest

        request = GetMessageResourceRequest.builder() \
            .message_id(msg_id) \
            .file_key(file_key) \
            .type("file") \
            .build()

        response = await asyncio.to_thread(
            self._client.im.v1.message_resource.get, request
        )
        if not response.success():
            logger.warning("file download failed: %s", response.msg)
            return None

        dest = dest_dir / (file_name or file_key)
        dest.write_bytes(response.file.read())
        return dest
